"""Customer-facing FP28 routes: order pages, check-order, products and site status."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from .. import config, slips, visitors
from ..config import ORDER_ID_PATTERN
from ..database import log_audit, open_db
from ..orders import (
    create_order,
    fetch_order_by_code,
    serialize_order,
    update_order,
    update_order_slip,
    validate_order_payload,
    validate_slip_payload,
)
from ..pages import ORDER_VIEW_HTML
from ..products import get_all_products
from ..sheets import sync_order_to_google_sheets
from ..site import get_schedule_status, get_site_settings
from ..slips import delete_local_slip_file, materialize_slip_payload
from ..timeutil import now_iso
from . import state

if TYPE_CHECKING:
    from .handler import OrderRequestHandler

SERVER_DIR = Path(__file__).resolve().parents[2]  # server/


def handle_get_health(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /health"""
    h.send_json(HTTPStatus.OK, {"status": "ok"})
    return


def handle_get_root(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /"""
    h.send_response(HTTPStatus.FOUND)
    h.send_header("Location", "/admin")
    h.send_header("Content-Length", "0")
    h.end_headers()
    return


def handle_get_docs(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /docs"""
    spec_url = "/openapi.yaml"
    swagger_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>SU Order API Docs</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({{url:"{spec_url}",dom_id:"#swagger-ui",presets:[SwaggerUIBundle.presets.apis,SwaggerUIBundle.SwaggerUIStandalonePreset],layout:"BaseLayout"}});</script>
</body></html>"""
    h.send_html(HTTPStatus.OK, swagger_html)
    return


def handle_get_openapi_yaml(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /openapi.yaml"""
    spec_path = SERVER_DIR / "openapi.yaml"
    try:
        content = spec_path.read_bytes()
        h.send_response(HTTPStatus.OK)
        h.send_header("Content-Type", "application/yaml")
        h.send_header("Content-Length", str(len(content)))
        h.end_headers()
        h.wfile.write(content)
    except FileNotFoundError:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "openapi.yaml not found"})
    return


def handle_get_orders(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /orders"""
    h.send_html(HTTPStatus.OK, ORDER_VIEW_HTML)
    return


def handle_get_check_order(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /check-order"""
    qs = parse_qs(urlparse(h.path).query)
    code = (qs.get("code") or [""])[0].strip().upper()
    student_code = (qs.get("studentCode") or [""])[0].strip()
    if not code and not student_code:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "code หรือ studentCode is required"})
        return
    with open_db() as connection:
        if code:
            row = fetch_order_by_code(connection, code)
            rows = [row] if row else []
        else:
            rows = [
                serialize_order(r)
                for r in connection.execute(
                    "SELECT * FROM orders WHERE student_code = ? ORDER BY internal_id DESC",
                    (student_code,),
                ).fetchall()
            ]
    if code and not rows:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "ไม่พบออเดอร์"})
        return
    def public_order(o: dict) -> dict:
        if isinstance(o, sqlite3.Row):
            o = serialize_order(o)
        return {
            "id": o.get("id"),
            "status": o.get("status"),
            "paymentStatus": o.get("paymentStatus"),
            "totalAmount": o.get("totalAmount"),
            "size": o.get("size"),
            "quantity": o.get("quantity"),
            "createdAt": o.get("createdAt"),
            "updatedAt": o.get("updatedAt"),
            "product": o.get("product"),
            "items": o.get("items"),
            "khantokTicket": o.get("khantokTicket"),
            "khantokTicketValue": o.get("khantokTicketValue"),
            "khantokTicketAlreadyClaimed": o.get("khantokTicketAlreadyClaimed"),
            "customer": {
                "fullName": (o.get("customer") or {}).get("fullName"),
                "studentCode": (o.get("customer") or {}).get("studentCode"),
                "school": (o.get("customer") or {}).get("school"),
            },
            "slip": {"uploadedAt": (o.get("slip") or {}).get("uploadedAt")} if o.get("slip") else None,
            "qrToken": o.get("qrToken") if o.get("status") == "shipped" else None,
        }
    if code:
        h.send_json(HTTPStatus.OK, {"order": public_order(rows[0])})
    else:
        h.send_json(HTTPStatus.OK, {"orders": [public_order(o) for o in rows]})
    return


def handle_get_products(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /products"""
    with open_db() as connection:
        h.send_json(HTTPStatus.OK, get_all_products(connection))
    return


def handle_get_site_settings(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /site-settings"""
    with open_db() as connection:
        data = get_site_settings(connection)
        data.pop("bypassToken", None)
        h.send_json(HTTPStatus.OK, data)
    return


def handle_get_site_status(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /site-status"""
    query = parse_qs(urlparse(h.path).query)
    key = visitors.visitor_key((query.get("vid") or [""])[0], (query.get("ip") or [""])[0])
    if key:
        visitors.record_visit(key)
    active_count = visitors.active_count()
    with open_db() as connection:
        rows = connection.execute(
            "SELECT key, value FROM site_settings WHERE key IN ('site_closed','schedule_enabled','schedule_warning_message','be_right_back_dates','be_right_back_active')"
        ).fetchall()
        s = {r["key"]: r["value"] for r in rows}
        site_closed = s.get("site_closed", "0") == "1"
        schedule_enabled = s.get("schedule_enabled", "0") == "1"
        warning_message = s.get("schedule_warning_message", "เว็บกำลังจะปิด กรุณาทำรายการให้เสร็จก่อนเวลา 22:59")
        be_right_back_dates_str = s.get("be_right_back_dates", "2026-05-24,2026-05-31,2026-06-07")
        be_right_back_dates = [d.strip() for d in be_right_back_dates_str.split(",") if d.strip()]
        be_right_back_active = s.get("be_right_back_active", "0") == "1"
        _TZ_BKK = timezone(timedelta(hours=7))
        today_bkk = datetime.now(_TZ_BKK).strftime("%Y-%m-%d")
        be_right_back = be_right_back_active or (today_bkk in be_right_back_dates)
    sched = get_schedule_status(schedule_enabled, warning_message)
    with state.lock:
        test_warn = time.time() < state.test_warning_until
    h.send_json(HTTPStatus.OK, {
        "siteClosed": site_closed,
        "scheduleEnabled": schedule_enabled,
        "scheduleClosed": sched["scheduleClosed"],
        "scheduleWarning": sched["scheduleWarning"] or test_warn,
        "scheduleWarningMessage": sched["scheduleWarningMessage"],
        "activeVisitors": active_count,
        "beRightBack": be_right_back,
    })
    return


def handle_get_product_images_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /product-images/([^/]+)"""
    raw_filename = match.group(1)
    safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "", raw_filename)
    if not safe_filename or safe_filename != raw_filename:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid filename"})
        return
    image_path = config.PRODUCT_IMAGES_DIR / safe_filename
    if not image_path.exists():
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "image not found"})
        return
    suffix = Path(safe_filename).suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    mime = mime_map.get(suffix, "image/jpeg")
    content = image_path.read_bytes()
    h.send_response(HTTPStatus.OK)
    h.send_header("Content-Type", mime)
    h.send_header("Content-Length", str(len(content)))
    h.send_header("Cache-Control", "public, max-age=86400")
    h.end_headers()
    h.wfile.write(content)
    return


def handle_get_orders_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /orders/([A-Z0-9-]+)"""
    order_code = match.group(1)
    if not ORDER_ID_PATTERN.match(order_code):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid order id"})
        return

    with open_db() as connection:
        row = fetch_order_by_code(connection, order_code)
        if row is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        # Require authorization: the master Bearer (attached by the Next.js proxy)
        # OR the per-order X-Order-Token. Without this, order codes are enumerable
        # and this endpoint would leak every customer's PII + qrToken.
        if not h.is_authorized_for_order(row):
            h.deny_unauthorized()
            return
        h.send_json(HTTPStatus.OK, serialize_order(row))


def handle_post_orders_by_id_feedback(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /orders/([A-Z0-9-]+)/feedback"""
    order_code = match.group(1)
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    if not isinstance(payload, dict):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid payload"})
        return
    try:
        rating = int(payload.get("rating", 0))
    except (TypeError, ValueError):
        rating = 0
    if rating < 1 or rating > 5:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "rating must be 1-5"})
        return
    comment = str(payload.get("comment") or "").strip()[:500]
    with open_db() as connection:
        existing_order = fetch_order_by_code(connection, order_code)
        if existing_order is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        if existing_order["status"] != "received":
            h.send_json(HTTPStatus.BAD_REQUEST, {"message": "order not received yet"})
            return
        existing_fb = connection.execute(
            "SELECT 1 FROM order_feedback WHERE order_code=?", (order_code,)
        ).fetchone()
        if existing_fb:
            h.send_json(HTTPStatus.CONFLICT, {"message": "feedback already submitted"})
            return
        connection.execute(
            "INSERT INTO order_feedback (order_code, rating, comment, created_at) VALUES (?, ?, ?, ?)",
            (order_code, rating, comment, now_iso()),
        )
        log_audit(connection, order_code, "feedback_submitted", f"rating={rating}")
        connection.commit()
    h.send_json(HTTPStatus.OK, {"ok": True})
    return


def handle_post_orders(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /orders"""
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return

    validated_payload, error_message = validate_order_payload(payload)
    if error_message:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": error_message})
        return

    with open_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        slugs = {item["product"]["slug"] for item in validated_payload.get("items", []) if isinstance(item.get("product"), dict)}
        slugs.add(validated_payload["product"]["slug"])
        for slug in slugs:
            row = connection.execute("SELECT available FROM products WHERE slug = ?", (slug,)).fetchone()
            if row and not row["available"]:
                h.send_json(HTTPStatus.BAD_REQUEST, {"message": f"สินค้า {slug} ไม่เปิดจำหน่ายในขณะนี้"})
                return
        order = create_order(connection, validated_payload)
        connection.commit()
        sync_order_to_google_sheets(order, "order_created")
        h.send_json(
            HTTPStatus.CREATED,
            {
                **order,
                "success": True,
                "khantokTicket": bool(order.get("khantokTicket")),
                "message": "ได้รับ Khantok ticket" if order.get("khantokTicket") else ("รับไปแล้ว " + str(order.get("customer", {}).get("studentCode", "") or "")) if order.get("khantokTicketAlreadyClaimed") else "สิทธิ์ Khantok ticket เต็มแล้ว",
            },
        )


def handle_put_orders_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PUT /orders/([A-Z0-9-]+)"""
    with open_db() as connection:
        existing_order = fetch_order_by_code(connection, match.group(1))
        if existing_order is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        if not h.is_authorized_for_order(existing_order):
            h.deny_unauthorized()
            return
        previous_slip_stored_name = existing_order["slip_stored_name"]
        previous_slip_storage_path = existing_order["slip_storage_path"]

        try:
            payload = h.read_json()
        except json.JSONDecodeError:
            h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
            return

        validated_payload, error_message = validate_order_payload(payload)
        if error_message:
            h.send_json(HTTPStatus.BAD_REQUEST, {"message": error_message})
            return

        order = update_order(connection, match.group(1), validated_payload)
        if order is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        connection.commit()
        delete_local_slip_file(previous_slip_stored_name, previous_slip_storage_path)
        sync_order_to_google_sheets(order, "order_updated")
        h.send_json(HTTPStatus.OK, order)


def handle_patch_orders_by_id_slip(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /orders/([A-Z0-9-]+)/slip"""
    with open_db() as connection:
        existing_order = fetch_order_by_code(connection, match.group(1))
        if existing_order is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        # Require the master Bearer or the per-order X-Order-Token before accepting
        # a slip, matching the do_PUT /orders/{code} path. Without it, anyone could
        # overwrite a pending order's slip and trigger server-side OCR unauthenticated.
        if not h.is_authorized_for_order(existing_order):
            h.deny_unauthorized()
            return
        if existing_order["status"] != "pending_payment":
            h.send_json(HTTPStatus.FORBIDDEN, {"message": "slip upload is only allowed for pending_payment orders"})
            return
        previous_slip_stored_name = existing_order["slip_stored_name"]
        previous_slip_storage_path = existing_order["slip_storage_path"]

        content_type = h.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            materialized_slip, error_message = h.read_multipart_slip(match.group(1))
            if error_message:
                h.send_json(HTTPStatus.BAD_REQUEST, {"message": error_message})
                return
        else:
            try:
                payload = h.read_json()
            except json.JSONDecodeError:
                h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
                return

            slip_payload, error_message = validate_slip_payload(payload)
            if error_message:
                h.send_json(HTTPStatus.BAD_REQUEST, {"message": error_message})
                return

            materialized_slip, error_message = materialize_slip_payload(match.group(1), slip_payload)
            if error_message:
                h.send_json(HTTPStatus.BAD_REQUEST, {"message": error_message})
                return

        order = update_order_slip(connection, match.group(1), materialized_slip)
        if order is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        connection.commit()
        if (
            materialized_slip is not None
            and materialized_slip.get("storedName") != previous_slip_stored_name
        ):
            delete_local_slip_file(previous_slip_stored_name, previous_slip_storage_path)
        slips.trigger_ocr_async(match.group(1), materialized_slip.get("storedPath") if materialized_slip else None, order.get("totalAmount"))
        sync_order_to_google_sheets(order, "payment_slip_uploaded")
        h.send_json(HTTPStatus.OK, order)
