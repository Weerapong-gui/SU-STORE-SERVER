"""The order-api request handler: shared request/response helpers, auth checks and dispatch."""
from __future__ import annotations

import hmac
import html
import json
import sqlite3
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from store import api as store_api

from .. import config
from ..database import open_db
from ..slips import persist_slip_file, sanitize_file_name
from ..timeutil import TZ_BANGKOK, now_iso
from .routes import ROUTES


class OrderRequestHandler(BaseHTTPRequestHandler):
    server_version = "SUOrderAPI/1.0"

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        response_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def send_html(self, status: int, body: str) -> None:
        response_body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(response_body)

    def send_file(self, path: Path, mime_type: str, file_name: str) -> None:
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            self.send_json(HTTPStatus.NOT_FOUND, {"message": "slip file not found"})
            return

        safe_file_name = sanitize_file_name(Path(file_name).name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header(
            "Content-Disposition",
            f'inline; filename="{html.escape(safe_file_name, quote=True)}"',
        )
        self.end_headers()
        self.wfile.write(content)

    def read_json(self) -> Any:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return None
        raw_body = self.rfile.read(content_length)
        return json.loads(raw_body.decode("utf-8"))

    def read_multipart_slip(self, order_code: str) -> tuple[dict[str, Any] | None, str | None]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            return None, "multipart/form-data content type is required"

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return None, "request body is empty"

        raw_body = self.rfile.read(content_length)
        message = BytesParser(policy=default).parsebytes(
            (
                f"Content-Type: {content_type}\r\n"
                "MIME-Version: 1.0\r\n"
                "\r\n"
            ).encode()
            + raw_body
        )

        if not message.is_multipart():
            return None, "multipart form data is invalid"

        slip_filename = ""
        slip_mime_type = "application/octet-stream"
        file_content = b""
        uploaded_at = now_iso()
        slip_count = 0

        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue

            field_name = str(part.get_param("name", header="content-disposition") or "")
            filename = part.get_filename()

            if field_name != "slip":
                continue

            slip_count += 1
            if slip_count > 1:
                return None, "multiple slip files are not supported"

            if not filename:
                return None, "slip filename is required"

            slip_filename = str(filename)
            slip_mime_type = str(part.get_content_type() or "application/octet-stream")
            file_content = part.get_payload(decode=True) or b""

        if slip_count == 0:
            return None, "slip is required"

        return persist_slip_file(
            order_code,
            slip_filename,
            slip_mime_type,
            uploaded_at,
            file_content,
        )

    def has_global_authorization(self) -> bool:
        incoming = self.headers.get("Authorization", "")
        expected = f"Bearer {config.ORDER_API_TOKEN}"
        return bool(config.ORDER_API_TOKEN) and hmac.compare_digest(incoming, expected)

    def has_claim_station_authorization(self) -> bool:
        incoming = self.headers.get("Authorization", "")
        if not incoming.startswith("Claim "):
            return False
        token = incoming[6:]
        with open_db() as conn:
            row = conn.execute("SELECT 1 FROM admin_users WHERE claim_token=?", (token,)).fetchone()
        return row is not None

    def get_claim_station_user(self):
        incoming = self.headers.get("Authorization", "")
        if not incoming.startswith("Claim "):
            return None
        token = incoming[6:]
        with open_db() as conn:
            row = conn.execute(
                "SELECT username FROM admin_users WHERE claim_token=?",
                (token,),
            ).fetchone()
        return row["username"] if row else None

    def has_superadmin_authorization(self) -> bool:
        incoming = self.headers.get("Authorization", "")
        if not incoming.startswith("Superadmin "):
            return False
        token = incoming[11:]
        with open_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM admin_users WHERE super_token=? AND is_superadmin=1",
                (token,),
            ).fetchone()
        return row is not None

    def is_authorized_for_order(self, row: sqlite3.Row) -> bool:
        if self.has_global_authorization():
            return True
        incoming = self.headers.get("X-Order-Token") or ""
        expected = row["access_token"] or ""
        return bool(expected) and hmac.compare_digest(incoming, expected)

    def deny_unauthorized(self) -> bool:
        self.send_json(HTTPStatus.UNAUTHORIZED, {"message": "unauthorized"})
        return False

    def require_admin_authorization(self) -> bool:
        if self.has_global_authorization():
            return True
        self.deny_unauthorized()
        return False

    def log_message(self, format: str, *args: Any) -> None:
        timestamp = datetime.now(TZ_BANGKOK).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {self.client_address[0]} {format % args}")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        if path.startswith("/v2/"):
            store_api.handle(self, method)
            return
        for route in ROUTES[method]:
            match = route.match(path)
            if match:
                route.handler(self, path, match if route.regex else None)
                return
        self.send_json(HTTPStatus.NOT_FOUND, {"message": "not found"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")
