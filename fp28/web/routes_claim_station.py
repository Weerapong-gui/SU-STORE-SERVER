"""Claim station routes (staff `Claim <token>` auth): search, slips, mark received."""
from __future__ import annotations

import hmac
import json
import re
from datetime import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .. import config
from ..auth import compute_claim_token, compute_qr_token
from ..database import cache_get, cache_invalidate, cache_set, log_audit, open_db
from ..display import enqueue_display2
from ..orders import fetch_order_by_code, mark_khantok_station_claimed, serialize_order
from ..pages import CLAIM_STATION_HTML
from ..slips import resolve_stored_slip_path
from ..timeutil import TZ_BANGKOK, now_iso

if TYPE_CHECKING:
    from .handler import OrderRequestHandler

def handle_get_claim_station(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /claim-station"""
    h.send_html(HTTPStatus.OK, CLAIM_STATION_HTML)
    return


def handle_get_claim_station_stats(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /claim-station/stats"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    with open_db() as connection:
        today_bkk = datetime.now(TZ_BANGKOK).strftime("%Y-%m-%d")
        received_today = connection.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status='received' AND DATE(received_at,'+7 hours')=?",
            (today_bkk,),
        ).fetchone()["c"]
        received_total = connection.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status='received'"
        ).fetchone()["c"]
        pending_pickup = connection.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status='shipped'"
        ).fetchone()["c"]
    h.send_json(HTTPStatus.OK, {
        "receivedToday": received_today,
        "receivedTotal": received_total,
        "pendingPickup": pending_pickup,
    })
    return


def handle_get_claim_station_orders(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /claim-station/orders"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    from urllib.parse import parse_qs
    qs = parse_qs(urlparse(h.path).query)
    student_code = (qs.get("studentCode") or [""])[0].strip()
    phone = (qs.get("phone") or [""])[0].strip()
    full_name = (qs.get("name") or [""])[0].strip()
    if not student_code and not phone and not full_name:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "studentCode, phone หรือ name is required"})
        return
    with open_db() as connection:
        if student_code:
            rows = connection.execute(
                "SELECT * FROM orders WHERE student_code=? AND status IN ('shipped','refund','received') ORDER BY created_at DESC",
                (student_code,),
            ).fetchall()
            not_found_msg = "ไม่พบออเดอร์พร้อมรับสำหรับรหัสนักศึกษานี้"
        elif phone:
            rows = connection.execute(
                "SELECT * FROM orders WHERE phone=? AND status IN ('shipped','refund','received') ORDER BY created_at DESC",
                (phone,),
            ).fetchall()
            not_found_msg = "ไม่พบออเดอร์พร้อมรับสำหรับเบอร์โทรนี้"
        else:
            pat = f"%{full_name}%"
            rows = connection.execute(
                "SELECT * FROM orders WHERE (full_name LIKE ? OR (first_name || ' ' || last_name) LIKE ?) AND status IN ('shipped','refund','received') ORDER BY created_at DESC",
                (pat, pat),
            ).fetchall()
            not_found_msg = "ไม่พบออเดอร์พร้อมรับสำหรับชื่อนี้"
    if not rows:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": not_found_msg})
        return
    h.send_json(HTTPStatus.OK, {"orders": [serialize_order(r) for r in rows]})
    return


def handle_get_claim_station_orders_pending(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /claim-station/orders-pending"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    with open_db() as connection:
        rows = connection.execute(
            "SELECT * FROM orders WHERE status='shipped' ORDER BY created_at ASC"
        ).fetchall()
    h.send_json(HTTPStatus.OK, {"orders": [serialize_order(r) for r in rows]})
    return


def handle_get_claim_station_orders_by_id_slip(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /claim-station/orders/([A-Z0-9-]+)/slip"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    order_code = match.group(1)
    with open_db() as connection:
        row = fetch_order_by_code(connection, order_code)
    if row is None:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "ไม่พบออเดอร์"})
        return
    slip_path = resolve_stored_slip_path(row["slip_stored_name"], row["slip_storage_path"] if "slip_storage_path" in row.keys() else None)  # noqa: SIM118 (sqlite3.Row)
    if slip_path is None or not slip_path.exists():
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "ไม่พบสลิป"})
        return
    mime = row["slip_mime_type"] or "image/jpeg"
    data = slip_path.read_bytes()
    h.send_response(HTTPStatus.OK)
    h.send_header("Content-Type", mime)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "private, max-age=300")
    h.end_headers()
    h.wfile.write(data)
    return


def handle_get_claim_station_orders_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /claim-station/orders/([A-Z0-9-]+)"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    from urllib.parse import parse_qs
    qs = parse_qs(urlparse(h.path).query)
    qr_token = (qs.get("qrToken") or [""])[0].strip()
    order_code = match.group(1)
    if qr_token and config.ORDER_API_TOKEN:
        expected = compute_qr_token(order_code)
        if not hmac.compare_digest(qr_token, expected):
            h.send_json(HTTPStatus.FORBIDDEN, {"message": "QR code ไม่ถูกต้อง — สแกนใหม่อีกครั้ง"})
            return
    cache_key = f"cs_order:{order_code}"
    cached = cache_get(cache_key)
    if cached is not None:
        h.send_json(HTTPStatus.OK, cached)
        return
    with open_db() as connection:
        row = fetch_order_by_code(connection, order_code)
        ocr_row = connection.execute(
            "SELECT amount, date, sender, bank, status, ref_number FROM slip_ocr_results WHERE order_code = ?",
            (order_code,),
        ).fetchone()
    if row is None:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "ไม่พบออเดอร์"})
        return
    order_data = serialize_order(row)
    if ocr_row:
        order_data["slipOcr"] = {
            "amount": ocr_row["amount"],
            "date": ocr_row["date"],
            "sender": ocr_row["sender"],
            "bank": ocr_row["bank"],
            "status": ocr_row["status"],
            "refNumber": ocr_row["ref_number"],
        }
    response = {"order": order_data}
    cache_set(cache_key, response)
    h.send_json(HTTPStatus.OK, response)
    return


def handle_post_claim_station_login(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /claim-station/login"""
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
    token = compute_claim_token(username, password)
    with open_db() as conn:
        row = conn.execute("SELECT 1 FROM admin_users WHERE username=? AND claim_token=?", (username, token)).fetchone()
    if row:
        h.send_json(HTTPStatus.OK, {"token": token, "username": username})
    else:
        h.send_json(HTTPStatus.UNAUTHORIZED, {"message": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"})
    return


def handle_patch_claim_station_orders_by_id_received(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /claim-station/orders/([A-Z0-9-]+)/received"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    order_code = match.group(1)
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        payload = {}
    received_by = (payload or {}).get("receivedBy", "") if isinstance(payload, dict) else ""
    with open_db() as connection:
        ts = now_iso()
        existing = fetch_order_by_code(connection, order_code)
        if existing is None:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "ไม่พบออเดอร์"})
            return
        prev_status = existing["status"]
        if prev_status == "received":
            h.send_json(HTTPStatus.CONFLICT, {"message": "ออเดอร์นี้ถูกรับสินค้าไปแล้ว", "alreadyReceived": True})
            return
        if prev_status == "refunded":
            h.send_json(HTTPStatus.CONFLICT, {"message": "ออเดอร์นี้คืนเงินไปแล้ว", "alreadyReceived": True})
            return
        if prev_status == "shipped":
            connection.execute(
                "UPDATE orders SET status='received', payment_status='paid', received_at=?, received_by=?, updated_at=?"
                " WHERE order_code=? AND status='shipped'",
                (ts, received_by or "staff", ts, order_code),
            )
            log_audit(connection, order_code, "claim_received", f"received by {received_by or 'staff'}")
        elif prev_status == "refund":
            log_audit(connection, order_code, "claim_refund_init",
                      f"queued to headband station by {received_by or 'staff'}")
        else:
            h.send_json(HTTPStatus.BAD_REQUEST, {"message": f"ออเดอร์มีสถานะ '{prev_status}' ยังไม่พร้อมรับ"})
            return
        updated_row = fetch_order_by_code(connection, order_code)
        claim_user = h.get_claim_station_user() or received_by or "staff"
        try:
            enqueue_display2(connection, updated_row, claim_user)
        except Exception as exc:
            log_audit(connection, order_code,
                      "display2_enqueue_failed", f"{type(exc).__name__}: {exc}")
        connection.commit()
        updated = updated_row
    cache_invalidate(f"cs_order:{order_code}")
    h.send_json(HTTPStatus.OK, {"order": serialize_order(updated), "prevStatus": prev_status})
    return


def handle_patch_claim_station_orders_by_id_khantok_claimed(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /claim-station/orders/([A-Z0-9-]+)/khantok-claimed"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    order_code = match.group(1)
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        payload = {}
    claimed_by = (payload or {}).get("claimedBy", "") if isinstance(payload, dict) else ""
    with open_db() as connection:
        claim_user = h.get_claim_station_user() or claimed_by or "staff"
        status_code, body = mark_khantok_station_claimed(connection, order_code, claim_user)
        if status_code == HTTPStatus.OK:
            connection.commit()
        else:
            # _PooledConnection.__exit__ auto-commits on clean exit; roll back
            # explicitly so failure paths can never persist stray writes.
            connection.rollback()
    cache_invalidate(f"cs_order:{order_code}")
    h.send_json(status_code, body)
    return


def handle_patch_claim_station_orders_by_id_name(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """PATCH /claim-station/orders/([A-Z0-9-]+)/name"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    order_code = match.group(1)
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    new_name = str((payload or {}).get("fullName", "")).strip() if isinstance(payload, dict) else ""
    if not new_name:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "fullName required"})
        return
    with open_db() as connection:
        ts = now_iso()
        cursor = connection.execute(
            "UPDATE orders SET full_name=?, updated_at=? WHERE order_code=?",
            (new_name, ts, order_code),
        )
        if cursor.rowcount == 0:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "ไม่พบออเดอร์"})
            return
        connection.execute(
            "UPDATE display2_picks SET full_name=? WHERE order_code=?",
            (new_name, order_code),
        )
        log_audit(connection, order_code, "claim_rename", f"name → {new_name}")
        connection.commit()
    cache_invalidate(f"cs_order:{order_code}")
    h.send_json(HTTPStatus.OK, {"ok": True})
    return
