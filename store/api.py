"""HTTP routes for SU STORE v2 (`/v2/*`).

`order_api.OrderRequestHandler` forwards every `/v2/` request to `handle()`. Shared
infrastructure (DB pool, audit log, staff tokens, slip and image storage) comes from
the `fp28` package.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import re
import secrets
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from core import config, slips
from core.auth import compute_claim_token
from core.database import log_audit, open_db
from core.product_images import save_product_image_file
from core.slips import delete_local_slip_file, resolve_stored_slip_path

from . import categories, dashboard, db, home, receipt_routes
from . import meta as store_meta
from .route_helpers import admin_user, content_length, is_admin, is_superadmin, json_body

_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def handle(handler: Any, method: str) -> None:
    parsed = urlparse(handler.path)
    path = parsed.path.rstrip("/") or "/"
    query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
    try:
        _route(handler, method, path, query)
    except db.StoreError as error:
        handler.send_json(error.status, {"message": error.message})
    except json.JSONDecodeError:
        handler.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})


def _route(handler: Any, method: str, path: str, query: dict[str, str]) -> None:
    send = handler.send_json

    # ── Public ────────────────────────────────────────────────────────────────
    if method == "GET" and path in ("/v2/meta", "/v2/admin/meta"):
        with open_db() as conn:
            return send(HTTPStatus.OK, store_meta.meta(conn))

    if method == "GET" and path == "/v2/categories":
        with open_db() as conn:
            return send(HTTPStatus.OK, {"categories": categories.list_categories(conn, public=True)})

    m = re.fullmatch(r"/v2/categories/([a-z0-9-]+)", path)
    if method == "GET" and m:
        with open_db() as conn:
            category = categories.get_category(conn, m.group(1), public=True)
            products = db.list_products(conn, public=True, category_id=category["id"])
            return send(HTTPStatus.OK, {"category": category, "products": products})

    if method == "GET" and path == "/v2/products":
        with open_db() as conn:
            return send(HTTPStatus.OK, {"products": db.list_products(conn, public=True)})

    m = re.fullmatch(r"/v2/products/([a-z0-9-]+)", path)
    if method == "GET" and m:
        with open_db() as conn:
            return send(HTTPStatus.OK, db.get_product(conn, m.group(1), public=True))

    if method == "GET" and path == "/v2/home":
        with open_db() as conn:
            return send(HTTPStatus.OK, home.resolve_home(conn, home.get_home_admin(conn)["published"]))

    if method == "POST" and path == "/v2/orders":
        payload = json_body(handler)
        with open_db() as conn:
            order = db.create_order(conn, payload)
            log_audit(conn, order["orderCode"], "v2_order_created", f"total={order['totalAmount']}")
        return send(HTTPStatus.CREATED, order)

    if method == "POST" and path == "/v2/orders/lookup":
        payload = json_body(handler)
        with open_db() as conn:
            return send(HTTPStatus.OK, db.lookup_order(conn, payload.get("orderCode"), payload.get("phone")))

    m = re.fullmatch(r"/v2/orders/(SU\d{4}-\d{4,})", path)
    if method == "GET" and m:
        with open_db() as conn:
            row = db.get_order_row(conn, m.group(1))
            _require_order_access(handler, row, query)
            return send(HTTPStatus.OK, db.get_order(conn, m.group(1)))

    m = re.fullmatch(r"/v2/orders/(SU\d{4}-\d{4,})/slip", path)
    if method == "POST" and m:
        code = m.group(1)
        with open_db() as conn:
            row = db.get_order_row(conn, code)
            _require_order_access(handler, row, query)
        if content_length(handler) > config.MAX_SLIP_SIZE_BYTES + 64 * 1024:
            raise db.StoreError(HTTPStatus.BAD_REQUEST, "ไฟล์สลิปต้องไม่เกิน 5MB")
        slip, error = handler.read_multipart_slip(code)
        if error:
            raise db.StoreError(HTTPStatus.BAD_REQUEST, error)
        with open_db() as conn:
            old_stored, old_path = row["slip_stored_name"], row["slip_storage_path"]
            order = db.attach_slip(conn, code, slip)
            log_audit(conn, code, "v2_slip_uploaded", slip["storedName"])
        delete_local_slip_file(old_stored, old_path)
        slips.trigger_ocr_async(code, slip["storedPath"], float(order["totalAmount"]))
        return send(HTTPStatus.OK, order)

    # ── Admin ─────────────────────────────────────────────────────────────────
    if method == "POST" and path == "/v2/admin/login":
        payload = json_body(handler)
        username, password = payload.get("username"), payload.get("password")
        if not (isinstance(username, str) and username and isinstance(password, str) and password):
            raise db.StoreError(HTTPStatus.BAD_REQUEST, "กรุณากรอกชื่อผู้ใช้และรหัสผ่าน")
        token = compute_claim_token(username, password)
        with open_db() as conn:
            row = conn.execute(
                "SELECT claim_token, is_superadmin FROM admin_users WHERE username = ?", (username,)
            ).fetchone()
        if row is None or not hmac.compare_digest(row["claim_token"], token):
            raise db.StoreError(HTTPStatus.UNAUTHORIZED, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
        return send(HTTPStatus.OK, {"token": token, "username": username, "isSuperadmin": bool(row["is_superadmin"])})

    if not path.startswith("/v2/admin/"):
        raise db.StoreError(HTTPStatus.NOT_FOUND, "not found")
    if not is_admin(handler):
        raise db.StoreError(HTTPStatus.UNAUTHORIZED, "unauthorized")

    if method == "GET" and path == "/v2/admin/me":
        return send(HTTPStatus.OK, {"username": admin_user(handler), "isSuperadmin": is_superadmin(handler)})

    if receipt_routes.route(handler, method, path):
        return None

    if method == "GET" and path == "/v2/admin/dashboard":
        with open_db() as conn:
            return send(HTTPStatus.OK, dashboard.dashboard(conn, _int(query.get("days"), 30)))

    if path == "/v2/admin/settings":
        if method == "GET":
            with open_db() as conn:
                return send(HTTPStatus.OK, db.get_settings(conn))
        if method in ("PUT", "PATCH"):
            payload = json_body(handler)
            user = admin_user(handler)
            with open_db() as conn:
                before = db.get_settings(conn)
                settings = db.update_settings(conn, payload)
                if "siteClosed" in payload:
                    log_audit(conn, "-", "v2_site_closed" if payload["siteClosed"] else "v2_site_opened",
                                   f"by {user}")
                # Who changed the payee account matters most: it decides where customers' money goes.
                if settings["payment"] != before["payment"]:
                    log_audit(conn, "-", "v2_payment_changed",
                                   f"{_payment_text(before)} -> {_payment_text(settings)} by {user}")
                if settings["announcement"] != before["announcement"]:
                    log_audit(conn, "-", "v2_announcement_changed", f"by {user}")
            return send(HTTPStatus.OK, settings)

    if path == "/v2/admin/home":
        if method == "GET":
            with open_db() as conn:
                return send(HTTPStatus.OK, home.get_home_admin(conn))
        if method == "PUT":
            payload = json_body(handler)
            with open_db() as conn:
                return send(HTTPStatus.OK, home.save_home_draft(conn, payload))

    if method == "GET" and path == "/v2/admin/home/preview":
        with open_db() as conn:
            return send(HTTPStatus.OK, home.resolve_home(conn, home.get_home_admin(conn)["draft"]))

    if method == "POST" and path == "/v2/admin/home/publish":
        with open_db() as conn:
            state = home.publish_home(conn)
            log_audit(conn, "-", "v2_home_published", f"by {admin_user(handler)}")
        return send(HTTPStatus.OK, state)

    if method == "POST" and path == "/v2/admin/home/discard":
        with open_db() as conn:
            return send(HTTPStatus.OK, home.discard_home_draft(conn))

    if method == "POST" and path in ("/v2/admin/home/images", "/v2/admin/images"):
        prefix = "home" if path == "/v2/admin/home/images" else "admin"
        return send(HTTPStatus.OK, {"url": _save_admin_image(handler, prefix)})

    if path == "/v2/admin/categories":
        if method == "GET":
            with open_db() as conn:
                return send(HTTPStatus.OK, {"categories": categories.list_categories(conn, public=False)})
        if method == "POST":
            payload = json_body(handler)
            with open_db() as conn:
                category = categories.create_category(conn, payload)
                log_audit(conn, "-", "v2_category_created", f"{category['slug']} by {admin_user(handler)}")
            return send(HTTPStatus.CREATED, category)

    if method == "PUT" and path == "/v2/admin/categories/order":
        payload = json_body(handler)
        with open_db() as conn:
            ordered = categories.reorder_categories(conn, payload.get("ids"))
            slugs = ",".join(category["slug"] for category in ordered)
            log_audit(conn, "-", "v2_category_reordered", f"{slugs} by {admin_user(handler)}")
        return send(HTTPStatus.OK, {"categories": ordered})

    m = re.fullmatch(r"/v2/admin/categories/(\d+)", path)
    if m:
        category_id = int(m.group(1))
        if method == "GET":
            with open_db() as conn:
                return send(HTTPStatus.OK, categories.get_category(conn, category_id, public=False))
        if method == "PUT":
            payload = json_body(handler)
            with open_db() as conn:
                category = categories.update_category(conn, category_id, payload)
                log_audit(conn, "-", "v2_category_updated", f"{category['slug']} by {admin_user(handler)}")
            return send(HTTPStatus.OK, category)
        if method == "DELETE":
            with open_db() as conn:
                category = categories.get_category(conn, category_id, public=False)
                result = categories.delete_category(conn, category_id)
                log_audit(conn, "-", "v2_category_deleted", f"{category['slug']} by {admin_user(handler)}")
            return send(HTTPStatus.OK, result)

    if path == "/v2/admin/products":
        if method == "GET":
            with open_db() as conn:
                return send(HTTPStatus.OK, {"products": db.list_products(conn, public=False)})
        if method == "POST":
            payload = json_body(handler)
            with open_db() as conn:
                return send(HTTPStatus.CREATED, db.create_product(conn, payload))

    m = re.fullmatch(r"/v2/admin/products/(\d+)", path)
    if m:
        product_id = int(m.group(1))
        if method == "GET":
            with open_db() as conn:
                return send(HTTPStatus.OK, db.get_product(conn, product_id, public=False))
        if method in ("PUT", "PATCH"):
            payload = json_body(handler)
            with open_db() as conn:
                return send(HTTPStatus.OK, db.update_product(conn, product_id, payload))
        if method == "DELETE":
            with open_db() as conn:
                return send(HTTPStatus.OK, db.delete_product(conn, product_id))

    m = re.fullmatch(r"/v2/admin/products/(\d+)/variants", path)
    if m and method == "PUT":
        payload = handler.read_json()
        with open_db() as conn:
            db.replace_variants(conn, int(m.group(1)), payload)
            return send(HTTPStatus.OK, db.get_product(conn, int(m.group(1)), public=False))

    m = re.fullmatch(r"/v2/admin/products/(\d+)/images", path)
    if m and method == "POST":
        product_id = int(m.group(1))
        with open_db() as conn:
            product = db.get_product(conn, product_id, public=False)
        content, mime = _read_multipart_file(handler, "image", config.MAX_PRODUCT_IMAGE_SIZE_BYTES)
        if mime not in _IMAGE_EXT:
            raise db.StoreError(HTTPStatus.BAD_REQUEST, "รองรับเฉพาะรูป JPG, PNG หรือ WebP")
        filename, error = save_product_image_file(
            f"v2-{product['slug']}-{secrets.token_hex(4)}", f"image{_IMAGE_EXT[mime]}", mime, content
        )
        if error:
            raise db.StoreError(HTTPStatus.BAD_REQUEST, error)
        with open_db() as conn:
            return send(HTTPStatus.OK, db.add_product_image(conn, product_id, f"/product-images/{filename}"))

    if method == "GET" and path == "/v2/admin/orders":
        with open_db() as conn:
            result = db.list_orders(
                conn,
                status=query.get("status") or None,
                query=(query.get("q") or "").strip() or None,
                page=_int(query.get("page"), 1),
                per_page=_int(query.get("per_page"), 50),
            )
        return send(HTTPStatus.OK, result)

    if method == "GET" and path == "/v2/admin/orders.csv":
        with open_db() as conn:
            rows = db.orders_csv_rows(conn, query.get("status") or None)
        buffer = io.StringIO()
        csv.writer(buffer).writerows(rows)
        body = ("﻿" + buffer.getvalue()).encode("utf-8")  # BOM so Excel reads Thai
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/csv; charset=utf-8")
        handler.send_header("Content-Disposition", 'attachment; filename="su-store-orders.csv"')
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return None

    m = re.fullmatch(r"/v2/admin/orders/(SU\d{4}-\d{4,})", path)
    if m:
        code = m.group(1)
        if method == "GET":
            with open_db() as conn:
                order = db.get_order(conn, code)
                ocr = conn.execute(
                    "SELECT status, amount, raw_reason, duplicate_of FROM slip_ocr_results WHERE order_code = ?",
                    (code,),
                ).fetchone()
            order["slipCheck"] = dict(ocr) if ocr else None
            return send(HTTPStatus.OK, order)
        if method == "PATCH":
            payload = json_body(handler)
            with open_db() as conn:
                order, changed_from = db.update_order(conn, code, payload)
                if changed_from:
                    log_audit(conn, code, "v2_status_changed",
                                   f"{changed_from} -> {order['status']} by {admin_user(handler)}")
            return send(HTTPStatus.OK, order)

    m = re.fullmatch(r"/v2/admin/orders/(SU\d{4}-\d{4,})/slip", path)
    if m and method == "GET":
        with open_db() as conn:
            row = db.get_order_row(conn, m.group(1))
        if not row["slip_stored_name"]:
            raise db.StoreError(HTTPStatus.NOT_FOUND, "ยังไม่มีสลิป")
        target = resolve_stored_slip_path(row["slip_stored_name"], row["slip_storage_path"])
        if target is None:
            raise db.StoreError(HTTPStatus.NOT_FOUND, "ไม่พบไฟล์สลิป")
        return handler.send_file(target, row["slip_mime_type"], row["slip_original_name"] or "slip")

    raise db.StoreError(HTTPStatus.NOT_FOUND, "not found")


def _require_order_access(handler: Any, row: Any, query: dict[str, str]) -> None:
    if handler.has_global_authorization():
        return
    incoming = handler.headers.get("X-Order-Token") or query.get("token") or ""
    if not incoming or not hmac.compare_digest(incoming, row["access_token"]):
        raise db.StoreError(HTTPStatus.UNAUTHORIZED, "unauthorized")


def _payment_text(settings: dict[str, Any]) -> str:
    p = settings["payment"]
    return f"{p['bankName']} {p['accountNumber']} {p['accountName']}".strip()


def _int(value: str | None, fallback: int) -> int:
    try:
        return int(value) if value else fallback
    except ValueError:
        return fallback


def _save_admin_image(handler: Any, prefix: str) -> str:
    content, mime = _read_multipart_file(handler, "image", config.MAX_PRODUCT_IMAGE_SIZE_BYTES)
    if mime not in _IMAGE_EXT:
        raise db.StoreError(HTTPStatus.BAD_REQUEST, "รองรับเฉพาะรูป JPG, PNG หรือ WebP")
    filename, error = save_product_image_file(f"{prefix}-{secrets.token_hex(4)}", f"image{_IMAGE_EXT[mime]}", mime, content)
    if error:
        raise db.StoreError(HTTPStatus.BAD_REQUEST, error)
    return f"/product-images/{filename}"


def _read_multipart_file(handler: Any, field: str, max_bytes: int) -> tuple[bytes, str]:
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise db.StoreError(HTTPStatus.BAD_REQUEST, "multipart/form-data required")
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0 or length > max_bytes + 64 * 1024:
        raise db.StoreError(HTTPStatus.BAD_REQUEST, "ไฟล์ใหญ่เกินไป")
    raw = handler.rfile.read(length)
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw
    )
    if message.is_multipart():
        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") == field and part.get_filename():
                return part.get_payload(decode=True) or b"", str(part.get_content_type())
    raise db.StoreError(HTTPStatus.BAD_REQUEST, f"ต้องแนบไฟล์ {field}")
