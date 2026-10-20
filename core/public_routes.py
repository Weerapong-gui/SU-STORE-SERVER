"""Public endpoints that the storefront needs independently of legacy FP28."""
from __future__ import annotations

import re
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from store import db as store_db

from . import config, visitors
from .database import open_db

if TYPE_CHECKING:
    from .handler import OrderRequestHandler


def route(handler: OrderRequestHandler, method: str, path: str) -> bool:
    """Handle storefront public routes and return whether a route matched."""
    if method != "GET":
        return False

    if path == "/health":
        handler.send_json(HTTPStatus.OK, {"status": "ok"})
        return True

    if path == "/site-status":
        query = parse_qs(urlparse(handler.path).query)
        key = visitors.visitor_key(
            (query.get("vid") or [""])[0], (query.get("ip") or [""])[0]
        )
        if key:
            visitors.record_visit(key)
        with open_db() as conn:
            site_closed = store_db.is_site_closed(conn)
        # The storefront only supports manual closure. Ignore FP28 schedule and
        # "be right back" settings that may remain in a copied database.
        handler.send_json(
            HTTPStatus.OK,
            {
                "siteClosed": site_closed,
                "scheduleEnabled": False,
                "scheduleClosed": False,
                "scheduleWarning": False,
                "scheduleWarningMessage": "",
                "activeVisitors": visitors.active_count(),
                "beRightBack": False,
            },
        )
        return True

    match = re.fullmatch(r"/product-images/([^/]+)", path)
    if match:
        raw_filename = match.group(1)
        safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "", raw_filename)
        if not safe_filename or safe_filename != raw_filename:
            handler.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid filename"})
            return True
        image_path = config.PRODUCT_IMAGES_DIR / safe_filename
        if not image_path.exists():
            handler.send_json(HTTPStatus.NOT_FOUND, {"message": "image not found"})
            return True
        suffix = Path(safe_filename).suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        content = image_path.read_bytes()
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", mime_map.get(suffix, "image/jpeg"))
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "public, max-age=86400")
        handler.end_headers()
        handler.wfile.write(content)
        return True

    return False
