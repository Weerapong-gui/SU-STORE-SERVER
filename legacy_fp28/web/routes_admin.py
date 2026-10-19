"""FP28 admin routes (Bearer token, or Superadmin for user management)."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from core import slips
from core.auth import compute_claim_token, compute_super_token
from core.config import ORDER_STATUSES
from core.database import cache_invalidate, log_audit, open_db
from core.product_images import save_product_image_file
from core.slips import delete_local_slip_file, resolve_stored_slip_path
from core.timeutil import TZ_BANGKOK, now_iso

from .. import visitors
from ..backups import create_backup, delete_backup, get_backup, list_backups
from ..orders import fetch_order_by_code, list_orders, serialize_order, update_order_fields, update_order_status
from ..pages import ADMIN_HTML, RECEIPT_SVG
from ..products import toggle_product_available, update_product, update_product_image
from ..sheets import sync_order_to_google_sheets
from ..site import get_site_settings, upsert_site_setting
from ..stats import (
    _STATS_STREAM_MAX_SECONDS,
    create_orders_summary_cached,
    export_orders_csv,
    get_analytics,
    get_product_breakdown,
    get_stats_snapshot,
    stats_stream_acquire,
    stats_stream_release,
)
from . import state

if TYPE_CHECKING:
    from core.handler import OrderRequestHandler

def handle_get_admin_users(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/users"""
    if not h.has_superadmin_authorization():
        h.deny_unauthorized()
        return
    with open_db() as conn:
        rows = conn.execute(
            "SELECT username, is_superadmin, created_by, created_at FROM admin_users ORDER BY is_superadmin DESC, created_at ASC"
        ).fetchall()
    h.send_json(HTTPStatus.OK, {"users": [
        {"username": r["username"], "isSuperadmin": bool(r["is_superadmin"]),
         "createdBy": r["created_by"], "createdAt": r["created_at"]}
        for r in rows
    ]})
    return


def handle_get_admin(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin"""
    h.send_html(HTTPStatus.OK, ADMIN_HTML)
    return


def handle_get_admin_receipt_template(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/receipt-template"""
    h.send_response(HTTPStatus.OK)
    h.send_header("Content-Type", "image/svg+xml; charset=utf-8")
    h.send_header("Content-Length", str(len(RECEIPT_SVG)))
    h.end_headers()
    h.wfile.write(RECEIPT_SVG)
    return


def handle_get_admin_stats_stream(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/stats-stream"""
    if not h.require_admin_authorization():
        return
    if not stats_stream_acquire():
        h.send_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"message": "stats stream busy, try again later"},
        )
        return
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "keep-alive")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    last_hash = None
    tick = 0
    started_at = time.monotonic()
    try:
        while (time.monotonic() - started_at) < _STATS_STREAM_MAX_SECONDS:
            payload = {"visitors": visitors.active_count(), **get_stats_snapshot()}
            payload_hash = hash(json.dumps(payload, sort_keys=True))
            if payload_hash != last_hash:
                h.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                last_hash = payload_hash
            else:
                # heartbeat: ต้องเขียนทุก tick แม้ข้อมูลไม่เปลี่ยน ไม่งั้น client
                # ที่ปิดแท็บไปแล้วจะไม่ถูกตรวจเจอ (รู้ได้ทาง BrokenPipe ตอนเขียน
                # เท่านั้น) แล้ว thread นี้จะวนกิน CPU ไปจนกว่า process จะตาย
                h.wfile.write(b": ping\n\n")
            h.wfile.flush()
            time.sleep(1 if tick < 10 else 5)
            tick += 1
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        stats_stream_release()
    return


def handle_get_admin_orders(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/orders"""
    if not h.require_admin_authorization():
        return
    from urllib.parse import parse_qs as _parse_qs
    qs = _parse_qs(urlparse(h.path).query)
    page = max(1, int((qs.get("page") or ["1"])[0]))
    per_page = min(max(1, int((qs.get("per_page") or ["50"])[0])), 200)
    search = (qs.get("search") or [""])[0].strip()
    status_f = (qs.get("status") or [""])[0].strip()
    student_code_f = (qs.get("studentCode") or [""])[0].strip()
    round_f = int((qs.get("round") or ["0"])[0]) if (qs.get("round") or ["0"])[0].isdigit() else 0
    with open_db() as connection:
        orders, total = list_orders(connection, page=page, per_page=per_page, search=search, status_filter=status_f, round_filter=round_f, student_code_filter=student_code_f)
        summary = create_orders_summary_cached(connection, round_filter=round_f)
    h.send_json(
        HTTPStatus.OK,
        {
            "orders": orders,
            "summary": summary,
            "total": total,
            "page": page,
            "perPage": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        },
    )
    return


def handle_get_admin_orders_by_id_extra_slips_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    r"""GET /admin/orders/([A-Z0-9-]+)/extra-slips/(\d+)"""
    if not h.require_admin_authorization():
        return
    order_code = match.group(1)
    slip_id = int(match.group(2))
    with open_db() as connection:
        row = connection.execute(
            "SELECT * FROM order_extra_slips WHERE id = ? AND order_code = ?",
            (slip_id, order_code),
        ).fetchone()
    if row is None:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "extra slip not found"})
        return
    slip_path = resolve_stored_slip_path(row["stored_name"], row["storage_path"])
    if slip_path is None:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "file not found"})
        return
    h.send_file(
        slip_path,
        row["mime_type"] or "application/octet-stream",
        row["original_name"] or row["stored_name"] or "slip",
    )
    return


def handle_get_admin_orders_by_id_extra_slips(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/orders/([A-Z0-9-]+)/extra-slips"""
    if not h.require_admin_authorization():
        return
    order_code = match.group(1)
    with open_db() as connection:
        rows = connection.execute(
            "SELECT * FROM order_extra_slips WHERE order_code = ? ORDER BY id",
            (order_code,),
        ).fetchall()
    slips = [
        {
            "id": r["id"],
            "originalName": r["original_name"],
            "mimeType": r["mime_type"],
            "fileSize": r["file_size"],
            "uploadedAt": r["uploaded_at"],
            "url": f"/admin/orders/{order_code}/extra-slips/{r['id']}",
        }
        for r in rows
    ]
    h.send_json(HTTPStatus.OK, slips)
    return


def handle_get_admin_orders_by_id_slip_check(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/orders/([A-Z0-9-]+)/slip-check"""
    if not h.require_admin_authorization():
        return
    with open_db() as connection:
        row = connection.execute(
            "SELECT * FROM slip_ocr_results WHERE order_code = ?",
            (match.group(1),),
        ).fetchone()
    if row is None:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "no OCR data"})
        return
    h.send_json(HTTPStatus.OK, dict(row))
    return


def handle_get_admin_orders_by_id_slip(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/orders/([A-Z0-9-]+)/slip"""
    if not h.require_admin_authorization():
        return
    with open_db() as connection:
        row = fetch_order_by_code(connection, match.group(1))
    if row is None:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
        return
    slip_path = resolve_stored_slip_path(row["slip_stored_name"], row["slip_storage_path"])
    if slip_path is None:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "slip not found"})
        return
    h.send_file(
        slip_path,
        row["slip_mime_type"] or "application/octet-stream",
        row["slip_original_name"] or row["slip_stored_name"] or "slip",
    )
    return


def handle_get_admin_orders_export_csv(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/orders/export.csv"""
    if not h.require_admin_authorization():
        return
    with open_db() as connection:
        orders, _ = list_orders(connection, page=1, per_page=999999)
        ocr_rows = connection.execute(
            "SELECT order_code, status, ref_number FROM slip_ocr_results"
        ).fetchall()
    ocr_map = {r["order_code"]: {"status": r["status"], "ref_number": r["ref_number"]} for r in ocr_rows}
    csv_content = export_orders_csv(orders, ocr_map)
    filename = f"orders-{datetime.now(TZ_BANGKOK).date().isoformat()}.csv"
    response_body = ("﻿" + csv_content).encode("utf-8")
    h.send_response(HTTPStatus.OK)
    h.send_header("Content-Type", "text/csv; charset=utf-8")
    h.send_header("Content-Length", str(len(response_body)))
    h.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    h.end_headers()
    h.wfile.write(response_body)
    return


def handle_get_admin_product_breakdown(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/product-breakdown"""
    if not h.require_admin_authorization():
        return
    from urllib.parse import parse_qs as _parse_qs_pb
    qs = _parse_qs_pb(urlparse(h.path).query)
    category = (qs.get("category") or ["single"])[0].strip()
    if category not in ("single", "jacket", "headband"):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid category"})
        return
    breakdown_round = int((qs.get("round") or ["0"])[0]) if (qs.get("round") or ["0"])[0].isdigit() else 0
    with open_db() as connection:
        result = get_product_breakdown(connection, category, round_filter=breakdown_round)
    h.send_json(HTTPStatus.OK, result)
    return


def handle_get_admin_analytics(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/analytics"""
    if not h.require_admin_authorization():
        return
    with open_db() as connection:
        analytics = get_analytics(connection)
    h.send_json(HTTPStatus.OK, analytics)
    return


def handle_get_admin_audit_log(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/audit-log"""
    if not h.require_admin_authorization():
        return
    from urllib.parse import parse_qs as _parse_qs2
    qs = _parse_qs2(urlparse(h.path).query)
    order_code = (qs.get("orderCode") or [""])[0].strip().upper()
    student_code = (qs.get("studentCode") or [""])[0].strip()
    limit = min(max(1, int((qs.get("limit") or ["200"])[0])), 1000)
    with open_db() as connection:
        if order_code:
            rows = connection.execute(
                "SELECT * FROM order_audit_log WHERE order_code = ? ORDER BY created_at DESC LIMIT ?",
                (order_code, limit),
            ).fetchall()
        elif student_code:
            rows = connection.execute(
                """SELECT al.* FROM order_audit_log al
                           JOIN orders o ON o.order_code = al.order_code
                           WHERE o.student_code = ?
                           ORDER BY al.created_at DESC LIMIT ?""",
                (student_code, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM order_audit_log ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    h.send_json(HTTPStatus.OK, {
        "log": [
            {"id": row["id"], "orderCode": row["order_code"], "event": row["event"],
             "detail": row["detail"], "createdAt": row["created_at"]}
            for row in rows
        ]
    })
    return


def handle_get_admin_feedback(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/feedback"""
    if not h.require_admin_authorization():
        return
    from urllib.parse import parse_qs as _pqs_fb
    qs = _pqs_fb(urlparse(h.path).query)
    limit = min(max(1, int((qs.get("limit") or ["500"])[0])), 2000)
    with open_db() as connection:
        rows = connection.execute(
            """SELECT f.id, f.order_code, f.rating, f.comment, f.created_at,
                              o.full_name, o.student_code, o.school
                       FROM order_feedback f
                       LEFT JOIN orders o ON o.order_code = f.order_code
                       ORDER BY f.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        summary_row = connection.execute(
            "SELECT COUNT(*) AS total, AVG(rating) AS avg_rating FROM order_feedback"
        ).fetchone()
        dist_rows = connection.execute(
            "SELECT rating, COUNT(*) AS n FROM order_feedback GROUP BY rating"
        ).fetchall()
    distribution = {str(i): 0 for i in range(1, 6)}
    for r in dist_rows:
        distribution[str(int(r["rating"]))] = int(r["n"])
    h.send_json(HTTPStatus.OK, {
        "feedback": [
            {
                "id": row["id"],
                "orderCode": row["order_code"],
                "rating": int(row["rating"]),
                "comment": row["comment"] or "",
                "createdAt": row["created_at"],
                "fullName": row["full_name"] or "",
                "studentCode": row["student_code"] or "",
                "school": row["school"] or "",
            }
            for row in rows
        ],
        "summary": {
            "total": int(summary_row["total"] or 0),
            "avgRating": float(summary_row["avg_rating"]) if summary_row["avg_rating"] is not None else 0.0,
            "distribution": distribution,
        },
    })
    return


def handle_get_admin_site_settings(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/site-settings"""
    if not h.require_admin_authorization():
        return
    with open_db() as connection:
        h.send_json(HTTPStatus.OK, get_site_settings(connection))
    return


def handle_get_admin_backups(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /admin/backups"""
    if not h.require_admin_authorization():
        return
    with open_db() as connection:
        backups = list_backups(connection)
    h.send_json(HTTPStatus.OK, {"backups": backups})
    return


def handle_get_admin_backups_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    r"""GET /admin/backups/(\d+)"""
    if not h.require_admin_authorization():
        return
    backup_id = int(match.group(1))
    with open_db() as connection:
        backup = get_backup(connection, backup_id)
    if backup is None:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "backup not found"})
        return
    h.send_json(HTTPStatus.OK, backup)
    return


def handle_post_admin_users(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /admin/users"""
    if not h.has_superadmin_authorization():
        h.deny_unauthorized()
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    new_username = ((payload or {}).get("username") or "").strip() if isinstance(payload, dict) else ""
    new_password = ((payload or {}).get("password") or "") if isinstance(payload, dict) else ""
    if not new_username or not new_password:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "username and password required"})
        return
    if len(new_username) > 64 or len(new_password) > 128:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "username/password too long"})
        return
    claim_tok = compute_claim_token(new_username, new_password)
    with open_db() as conn:
        existing = conn.execute("SELECT 1 FROM admin_users WHERE username=?", (new_username,)).fetchone()
        if existing:
            h.send_json(HTTPStatus.CONFLICT, {"message": f"username '{new_username}' มีอยู่แล้ว"})
            return
        superadmin_row = conn.execute("SELECT username FROM admin_users WHERE super_token=? AND is_superadmin=1",
            (h.headers.get("Authorization","")[11:],)).fetchone()
        created_by = superadmin_row["username"] if superadmin_row else "superadmin"
        conn.execute(
            "INSERT INTO admin_users (username,claim_token,super_token,is_superadmin,created_by,created_at) VALUES (?,?,NULL,0,?,?)",
            (new_username, claim_tok, created_by, now_iso()),
        )
        conn.commit()
    h.send_json(HTTPStatus.OK, {"ok": True, "username": new_username})
    return


def handle_post_admin_orders_by_id_slip(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /admin/orders/([A-Z0-9-]+)/slip"""
    if not h.require_admin_authorization():
        return
    order_code = match.group(1)
    with open_db() as connection:
        existing_order = fetch_order_by_code(connection, order_code)
        if existing_order is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        previous_slip_stored_name = existing_order["slip_stored_name"]
        previous_slip_storage_path = existing_order["slip_storage_path"]
        materialized_slip, error_message = h.read_multipart_slip(order_code)
        if error_message:
            h.send_json(HTTPStatus.BAD_REQUEST, {"message": error_message})
            return
        connection.execute(
            """UPDATE orders SET slip_original_name=?, slip_stored_name=?, slip_storage_path=?,
                       slip_mime_type=?, slip_size=?, slip_uploaded_at=?, updated_at=? WHERE order_code=?""",
            (
                materialized_slip["originalName"], materialized_slip["storedName"],
                materialized_slip["storedPath"], materialized_slip["mimeType"],
                int(materialized_slip["size"]), materialized_slip["uploadedAt"],
                now_iso(), order_code,
            ),
        )
        log_audit(connection, order_code, "admin_slip_uploaded", "admin replaced slip")
        connection.commit()
        if previous_slip_stored_name and previous_slip_stored_name != materialized_slip["storedName"]:
            delete_local_slip_file(previous_slip_stored_name, previous_slip_storage_path)
        row = fetch_order_by_code(connection, order_code)
    serialized = serialize_order(row)
    slips.trigger_ocr_async(order_code, materialized_slip.get("storedPath"), serialized.get("totalAmount"))
    h.send_json(HTTPStatus.OK, serialized)
    return


def handle_post_admin_orders_by_id_extra_slips(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /admin/orders/([A-Z0-9-]+)/extra-slips"""
    if not h.require_admin_authorization():
        return
    order_code = match.group(1)
    with open_db() as connection:
        if fetch_order_by_code(connection, order_code) is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        materialized_slip, error_message = h.read_multipart_slip(order_code)
        if error_message:
            h.send_json(HTTPStatus.BAD_REQUEST, {"message": error_message})
            return
        uploaded_at = now_iso()
        connection.execute(
            """INSERT INTO order_extra_slips
                       (order_code, original_name, stored_name, storage_path, mime_type, file_size, uploaded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                order_code,
                materialized_slip["originalName"],
                materialized_slip["storedName"],
                materialized_slip["storedPath"],
                materialized_slip["mimeType"],
                int(materialized_slip["size"]),
                uploaded_at,
            ),
        )
        new_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        log_audit(connection, order_code, "admin_extra_slip_uploaded", f"extra slip {new_id} uploaded")
        connection.commit()
    h.send_json(HTTPStatus.OK, {
        "id": new_id,
        "originalName": materialized_slip["originalName"],
        "mimeType": materialized_slip["mimeType"],
        "fileSize": int(materialized_slip["size"]),
        "uploadedAt": uploaded_at,
        "url": f"/admin/orders/{order_code}/extra-slips/{new_id}",
    })
    return


def handle_post_admin_superlogin(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /admin/superlogin"""
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    username = (payload or {}).get("username", "") if isinstance(payload, dict) else ""
    password = (payload or {}).get("password", "") if isinstance(payload, dict) else ""
    if not (isinstance(username, str) and username and isinstance(password, str) and password):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "username and password required"})
        return
    token = compute_super_token(username, password)
    with open_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM admin_users WHERE username=? AND super_token=? AND is_superadmin=1",
            (username, token),
        ).fetchone()
    if row:
        h.send_json(HTTPStatus.OK, {"token": token, "username": username})
    else:
        h.send_json(HTTPStatus.UNAUTHORIZED, {"message": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"})
    return


def handle_post_admin_test_warning(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /admin/test-warning"""
    if not h.require_admin_authorization():
        return
    with state.lock:
        state.test_warning_until = time.time() + 60
    h.send_json(HTTPStatus.OK, {"ok": True})
    return


def handle_post_admin_stop_test_warning(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /admin/stop-test-warning"""
    if not h.require_admin_authorization():
        return
    with state.lock:
        state.test_warning_until = 0
    h.send_json(HTTPStatus.OK, {"ok": True})
    return


def handle_post_admin_backups(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /admin/backups"""
    if not h.require_admin_authorization():
        return
    with open_db() as connection:
        backup = create_backup(connection)
    h.send_json(HTTPStatus.CREATED, backup)
    return


def handle_post_admin_products_by_id_image(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /admin/products/([a-z0-9-]+)/image"""
    if not h.require_admin_authorization():
        return
    slug = match.group(1)
    content_type = h.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "multipart/form-data required"})
        return
    content_length = int(h.headers.get("Content-Length", "0"))
    if content_length <= 0:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "empty body"})
        return
    raw_body = h.rfile.read(content_length)
    message = BytesParser(policy=default).parsebytes(
        (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw_body
    )
    if not message.is_multipart():
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid multipart data"})
        return
    image_content = b""
    image_filename = ""
    image_mime = ""
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        field_name = str(part.get_param("name", header="content-disposition") or "")
        if field_name != "image":
            continue
        filename = part.get_filename()
        if not filename:
            h.send_json(HTTPStatus.BAD_REQUEST, {"message": "image filename required"})
            return
        image_filename = str(filename)
        image_mime = str(part.get_content_type() or "image/jpeg")
        image_content = part.get_payload(decode=True) or b""
    if not image_content:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "image field is required"})
        return
    filename, error = save_product_image_file(slug, image_filename, image_mime, image_content)
    if error:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": error})
        return
    with open_db() as connection:
        product = update_product_image(connection, slug, f"/product-images/{filename}")
        if product is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "product not found"})
            return
        connection.commit()
    h.send_json(HTTPStatus.OK, product)
    return


def handle_put_admin_products_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PUT /admin/products/([a-z0-9-]+)"""
    if not h.require_admin_authorization():
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    if not isinstance(payload, dict):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid payload"})
        return
    with open_db() as connection:
        product = update_product(connection, match.group(1), payload)
        if product is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "product not found"})
            return
        connection.commit()
    h.send_json(HTTPStatus.OK, product)
    return


def handle_put_admin_users_by_id_password(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PUT /admin/users/([^/]+)/password"""
    if not h.has_superadmin_authorization():
        h.deny_unauthorized()
        return
    username = match.group(1)
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    new_password = ((payload or {}).get("password") or "") if isinstance(payload, dict) else ""
    if not new_password or len(new_password) < 4:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "รหัสผ่านต้องมีอย่างน้อย 4 ตัวอักษร"})
        return
    if len(new_password) > 128:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "รหัสผ่านยาวเกินไป"})
        return
    with open_db() as conn:
        row = conn.execute("SELECT is_superadmin FROM admin_users WHERE username=?", (username,)).fetchone()
        if not row:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": f"ไม่พบ user '{username}'"})
            return
        new_claim = compute_claim_token(username, new_password)
        if row["is_superadmin"]:
            new_super = compute_super_token(username, new_password)
            conn.execute(
                "UPDATE admin_users SET claim_token=?, super_token=? WHERE username=?",
                (new_claim, new_super, username),
            )
        else:
            conn.execute(
                "UPDATE admin_users SET claim_token=? WHERE username=?",
                (new_claim, username),
            )
        conn.commit()
    h.send_json(HTTPStatus.OK, {"ok": True, "username": username})
    return


def handle_patch_admin_orders_by_id_status(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /admin/orders/([A-Z0-9-]+)/status"""
    if not h.require_admin_authorization():
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, str) or status not in ORDER_STATUSES:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid status"})
        return
    with open_db() as connection:
        order = update_order_status(connection, match.group(1), status)
        if order is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        connection.commit()
    sync_order_to_google_sheets(order, "admin_status_updated")
    h.send_json(HTTPStatus.OK, order)
    return


def handle_patch_admin_orders_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /admin/orders/([A-Z0-9-]+)"""
    if not h.require_admin_authorization():
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    if not isinstance(payload, dict):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid payload"})
        return
    expected_updated_at = payload.pop("expectedUpdatedAt", None)
    with open_db() as connection:
        order = update_order_fields(connection, match.group(1), payload, expected_updated_at)
        if order is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        if order is False:
            h.send_json(HTTPStatus.CONFLICT, {"message": "ออเดอร์ถูกแก้ไขโดยผู้ใช้อื่นระหว่างที่คุณกำลังแก้ไข — กรุณารีโหลดและลองใหม่"})
            return
        connection.commit()
    cache_invalidate(f"cs_order:{match.group(1)}")
    h.send_json(HTTPStatus.OK, order)
    return


def handle_patch_admin_orders_bulk_status(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /admin/orders/bulk-status"""
    if not h.require_admin_authorization():
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    if not isinstance(payload, dict):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid payload"})
        return
    order_ids = payload.get("orderIds")
    status = payload.get("status")
    if not isinstance(order_ids, list) or not order_ids:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "orderIds must be a non-empty list"})
        return
    if not isinstance(status, str) or status not in ORDER_STATUSES:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid status"})
        return
    updated = []
    failed = []
    with open_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for oid in order_ids[:100]:
            order = update_order_status(connection, str(oid), status)
            if order:
                updated.append(oid)
            else:
                failed.append(oid)
        connection.commit()
    h.send_json(HTTPStatus.OK, {"updated": updated, "failed": failed})
    return


def handle_patch_admin_products_by_id_available(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /admin/products/([a-z0-9-]+)/available"""
    if not h.require_admin_authorization():
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    available = payload.get("available") if isinstance(payload, dict) else None
    if not isinstance(available, bool):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "available must be a boolean"})
        return
    with open_db() as connection:
        product = toggle_product_available(connection, match.group(1), available)
        if product is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "product not found"})
            return
        connection.commit()
    h.send_json(HTTPStatus.OK, product)
    return


def handle_patch_admin_site_settings(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /admin/site-settings"""
    if not h.require_admin_authorization():
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    if not isinstance(payload, dict):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid payload"})
        return
    with open_db() as connection:
        if "announcementBanner" in payload:
            upsert_site_setting(connection, "announcement_banner", str(payload["announcementBanner"]))
        if "announcementBannerEnabled" in payload:
            upsert_site_setting(connection, "announcement_banner_enabled", "1" if payload["announcementBannerEnabled"] else "0")
        if "storeOpen" in payload:
            upsert_site_setting(connection, "store_open", "1" if payload["storeOpen"] else "0")
        if "orderDeadline" in payload:
            upsert_site_setting(connection, "order_deadline", str(payload["orderDeadline"] or ""))
        if "phaseOverride" in payload:
            v = payload["phaseOverride"]
            upsert_site_setting(connection, "phase_override", str(int(v)) if v is not None else "")
        if "maxPhases" in payload:
            upsert_site_setting(connection, "max_phases", str(max(1, int(payload["maxPhases"]))))
        if "phaseConfigs" in payload and isinstance(payload["phaseConfigs"], dict):
            upsert_site_setting(connection, "phase_configs", json.dumps(payload["phaseConfigs"]))
        if "siteClosed" in payload:
            upsert_site_setting(connection, "site_closed", "1" if payload["siteClosed"] else "0")
        if "scheduleEnabled" in payload:
            upsert_site_setting(connection, "schedule_enabled", "1" if payload["scheduleEnabled"] else "0")
        if "scheduleWarningMessage" in payload:
            upsert_site_setting(connection, "schedule_warning_message", str(payload["scheduleWarningMessage"]))
        if "khantokQuota100" in payload:
            upsert_site_setting(connection, "khantok_quota_100", str(int(payload["khantokQuota100"])))
        if "khantokQuota50" in payload:
            upsert_site_setting(connection, "khantok_quota_50", str(int(payload["khantokQuota50"])))
        if "beRightBackDates" in payload:
            upsert_site_setting(connection, "be_right_back_dates", str(payload["beRightBackDates"]))
        if "beRightBackActive" in payload:
            upsert_site_setting(connection, "be_right_back_active", "1" if payload["beRightBackActive"] else "0")
        connection.commit()
        settings = get_site_settings(connection)
    h.send_json(HTTPStatus.OK, settings)
    return


def handle_delete_admin_users_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """DELETE /admin/users/([^/]+)"""
    if not h.has_superadmin_authorization():
        h.deny_unauthorized()
        return
    username = match.group(1)
    with open_db() as conn:
        row = conn.execute("SELECT is_superadmin FROM admin_users WHERE username=?", (username,)).fetchone()
        if not row:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": f"ไม่พบ user '{username}'"})
            return
        if row["is_superadmin"]:
            h.send_json(HTTPStatus.FORBIDDEN, {"message": "ไม่สามารถลบ superadmin ได้"})
            return
        conn.execute("DELETE FROM admin_users WHERE username=?", (username,))
        conn.commit()
    h.send_json(HTTPStatus.OK, {"ok": True})
    return


def handle_delete_admin_orders_by_id_slip(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """DELETE /admin/orders/([A-Z0-9-]+)/slip"""
    if not h.require_admin_authorization():
        return
    order_code = match.group(1)
    with open_db() as connection:
        row = fetch_order_by_code(connection, order_code)
        if row is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "order not found"})
            return
        stored_name = row["slip_stored_name"]
        stored_path = row["slip_storage_path"]
        if not stored_name:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "no slip to delete"})
            return
        connection.execute(
            """UPDATE orders SET slip_original_name=NULL, slip_stored_name=NULL,
                       slip_storage_path=NULL, slip_mime_type=NULL, slip_size=NULL,
                       slip_uploaded_at=NULL, updated_at=? WHERE order_code=?""",
            (now_iso(), order_code),
        )
        log_audit(connection, order_code, "admin_slip_deleted", "admin deleted slip")
        connection.commit()
        updated_row = fetch_order_by_code(connection, order_code)
    delete_local_slip_file(stored_name, stored_path)
    h.send_json(HTTPStatus.OK, serialize_order(updated_row))
    return


def handle_delete_admin_orders_by_id_extra_slips_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    r"""DELETE /admin/orders/([A-Z0-9-]+)/extra-slips/(\d+)"""
    if not h.require_admin_authorization():
        return
    order_code = match.group(1)
    slip_id = int(match.group(2))
    with open_db() as connection:
        row = connection.execute(
            "SELECT * FROM order_extra_slips WHERE id = ? AND order_code = ?",
            (slip_id, order_code),
        ).fetchone()
        if row is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "extra slip not found"})
            return
        connection.execute("DELETE FROM order_extra_slips WHERE id = ?", (slip_id,))
        log_audit(connection, order_code, "admin_extra_slip_deleted", f"extra slip {slip_id} deleted")
        connection.commit()
    delete_local_slip_file(row["stored_name"], row["storage_path"])
    h.send_json(HTTPStatus.OK, {"ok": True})
    return


def handle_delete_admin_backups_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    r"""DELETE /admin/backups/(\d+)"""
    if not h.require_admin_authorization():
        return
    backup_id = int(match.group(1))
    with open_db() as connection:
        found = delete_backup(connection, backup_id)
    if not found:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "backup not found"})
        return
    h.send_json(HTTPStatus.OK, {"ok": True})
    return
