"""Store receipt snapshots, settings and transactional numbering."""
from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .common import StoreError, clean_image_url, clean_text, now_iso
from .contract import CONTRACT

RECEIPT_ISSUABLE_STATUSES = tuple(CONTRACT["receiptIssuableStatuses"])
DEFAULT_LAYOUT: dict[str, Any] = CONTRACT["receiptDefaultLayout"]
LAYOUT_MAX = CONTRACT["receiptLayoutMax"]
FONT_SIZE_MIN = CONTRACT["receiptFontSizeMin"]
FONT_SIZE_MAX = CONTRACT["receiptFontSizeMax"]
DEFAULT_RECEIPT_SETTINGS: dict[str, Any] = {
    "payeeName": "", "signatureUrl": "", "prefix": "SU-R2569-", "layout": DEFAULT_LAYOUT,
}
_RECEIPT_SELECT = (
    "SELECT r.*, o.order_code FROM store_receipts r JOIN store_orders o ON o.id = r.order_id"
)


def _utc_now() -> str:
    return datetime.fromisoformat(now_iso()).astimezone(UTC).isoformat()


def _validate_layout(value: Any, template: Any = DEFAULT_LAYOUT, key: str = "layout") -> Any:
    if isinstance(template, dict):
        if not isinstance(value, dict) or value.keys() != template.keys():
            raise StoreError(400, "รูปแบบตำแหน่งใบเสร็จไม่ถูกต้อง")
        return {name: _validate_layout(value[name], default, name) for name, default in template.items()}
    if isinstance(template, str):
        if not isinstance(value, str) or value not in ("start", "middle", "end"):
            raise StoreError(400, "ตำแหน่งจัดแนวข้อความไม่ถูกต้อง")
    else:
        minimum, maximum = (FONT_SIZE_MIN, FONT_SIZE_MAX) if key == "size" else (0, LAYOUT_MAX)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not minimum <= value <= maximum or not math.isfinite(value)):
            raise StoreError(400, "ค่าตำแหน่งหรือขนาดตัวอักษรใบเสร็จไม่ถูกต้อง")
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Overlay stored settings on defaults so a field added to DEFAULT_LAYOUT later still has a value."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def get_receipt_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM store_settings WHERE key = 'receipt'").fetchone()
    defaults = copy.deepcopy(DEFAULT_RECEIPT_SETTINGS)
    return _deep_merge(defaults, json.loads(row[0])) if row else defaults


def save_receipt_settings(conn: sqlite3.Connection, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(400, "ข้อมูลไม่ถูกต้อง")
    settings = {**get_receipt_settings(conn), **payload}
    prefix = clean_text(settings["prefix"], "คำนำหน้าเลขใบเสร็จ", max_len=20)
    if not re.fullmatch(r"[A-Za-z0-9-]*", prefix):
        raise StoreError(400, "คำนำหน้าเลขใบเสร็จไม่ถูกต้อง")
    result = {
        "payeeName": clean_text(settings["payeeName"], "ชื่อผู้รับเงิน", max_len=80),
        "signatureUrl": clean_image_url(settings["signatureUrl"], "ลายเซ็น"),
        "prefix": prefix, "layout": _validate_layout(settings["layout"]),
    }
    conn.execute(
        "INSERT INTO store_settings (key, value, updated_at) VALUES ('receipt', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (json.dumps(result, ensure_ascii=False), _utc_now()),
    )
    return result


def baht_text(amount: int) -> str:
    """Port of claim_station.html toBahtText, preserving its wording."""
    if amount == 0:
        return "ศูนย์บาทถ้วน"
    ones = ("", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า")

    def under_hundred(value: int) -> str:
        if value < 10:
            return ones[value]
        tens, units = divmod(value, 10)
        text = "สิบ" if tens == 1 else "ยี่สิบ" if tens == 2 else ones[tens] + "สิบ"
        return text + ("เอ็ด" if units == 1 else ones[units])

    def under_thousand(value: int) -> str:
        hundreds, remainder = divmod(value, 100)
        return (ones[hundreds] + "ร้อย" if hundreds else "") + under_hundred(remainder)

    result = ""
    if amount >= 1000000:
        result += under_thousand(amount // 1000000) + "ล้าน"
        amount %= 1000000
    for scale, word in ((100000, "แสน"), (10000, "หมื่น"), (1000, "พัน")):
        if amount >= scale:
            result += ones[amount // scale] + word
            amount %= scale
    return result + under_thousand(amount) + "บาทถ้วน"


def _serialize(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "receiptNo": row["receipt_no"], "orderCode": row["order_code"],
        "issuedAt": row["issued_at"], "issuedBy": row["issued_by"], "snapshot": json.loads(row["snapshot_json"]),
        "cancelledAt": row["cancelled_at"], "cancelledBy": row["cancelled_by"], "cancelReason": row["cancel_reason"],
    }


def _order(conn: sqlite3.Connection, order_code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM store_orders WHERE order_code = ?", (order_code,)).fetchone()
    if row is None:
        raise StoreError(404, "ไม่พบออเดอร์")
    return row


def next_receipt_no(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT last_seq FROM store_counters WHERE period = 'receipt'").fetchone()
    seq = row[0] + 1 if row else 1
    return f"{get_receipt_settings(conn)['prefix']}{seq:04d}"


def _snapshot(conn: sqlite3.Connection, order: sqlite3.Row, number: str, issued_at: str,
              customer: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(customer, dict):
        raise StoreError(400, "ข้อมูลผู้ซื้อไม่ถูกต้อง")
    buyer = {key: clean_text(customer.get(key), label, max_len=120) for key, label in (
        ("name", "ชื่อผู้ซื้อ"), ("studentCode", "รหัสนักศึกษา"), ("school", "สำนักวิชา"),
    )}
    rows = conn.execute("SELECT * FROM store_order_items WHERE order_id = ? ORDER BY id", (order["id"],)).fetchall()
    lines = [{
        "no": index, "name": row["product_name"] + (" " + row["variant_label"] if row["variant_label"] != "-" else ""),
        "quantity": row["quantity"], "unitPrice": row["unit_price"], "amount": row["quantity"] * row["unit_price"],
    } for index, row in enumerate(rows, 1)]
    settings = get_receipt_settings(conn)
    return {
        "receiptNo": number, "issuedAt": issued_at, "customer": buyer, "lines": lines,
        "total": order["total_amount"], "totalText": baht_text(order["total_amount"]),
        "payeeName": settings["payeeName"], "signatureUrl": settings["signatureUrl"],
    }


def issue_receipt(conn: sqlite3.Connection, order_code: str, *, issued_by: str,
                  customer: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return `(receipt, created)`; `created` is False when the order already had an active receipt."""
    # Match create_order's transaction boundary; the lock covers the idempotency check too.
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        order = _order(conn, order_code)
        existing = conn.execute(
            _RECEIPT_SELECT + " WHERE r.order_id = ? AND r.cancelled_at IS NULL", (order["id"],)
        ).fetchone()
        if existing:
            conn.rollback()
            return _serialize(existing), False
        if order["status"] not in RECEIPT_ISSUABLE_STATUSES:
            raise StoreError(400, "ออกใบเสร็จได้เฉพาะออเดอร์ที่ชำระเงินแล้ว")
        number, issued_at = next_receipt_no(conn), _utc_now()
        snapshot = _snapshot(conn, order, number, issued_at, customer)
        conn.execute(
            "INSERT INTO store_counters (period, last_seq) VALUES ('receipt', 1) "
            "ON CONFLICT(period) DO UPDATE SET last_seq = last_seq + 1"
        )
        seq = conn.execute("SELECT last_seq FROM store_counters WHERE period = 'receipt'").fetchone()[0]
        cursor = conn.execute(
            "INSERT INTO store_receipts (receipt_no, seq, order_id, issued_at, issued_by, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (number, seq, order["id"], issued_at, issued_by, json.dumps(snapshot, ensure_ascii=False)),
        )
        row = conn.execute(_RECEIPT_SELECT + " WHERE r.id = ?", (cursor.lastrowid,)).fetchone()
        conn.commit()
        return _serialize(row), True
    except Exception:
        conn.rollback()
        raise


def cancel_receipt(conn: sqlite3.Connection, receipt_id: int, *, cancelled_by: str, reason: str) -> dict[str, Any]:
    reason = clean_text(reason, "เหตุผลที่ยกเลิก", required=True, max_len=200)
    cursor = conn.execute(
        "UPDATE store_receipts SET cancelled_at = ?, cancelled_by = ?, cancel_reason = ? "
        "WHERE id = ? AND cancelled_at IS NULL", (_utc_now(), cancelled_by, reason, receipt_id),
    )
    row = conn.execute(_RECEIPT_SELECT + " WHERE r.id = ?", (receipt_id,)).fetchone()
    if row is None:
        raise StoreError(404, "ไม่พบใบเสร็จ")
    if cursor.rowcount == 0:
        raise StoreError(400, "ใบเสร็จนี้ถูกยกเลิกแล้ว")
    return _serialize(row)


def get_receipt(conn: sqlite3.Connection, receipt_id: int) -> dict[str, Any]:
    row = conn.execute(_RECEIPT_SELECT + " WHERE r.id = ?", (receipt_id,)).fetchone()
    if row is None:
        raise StoreError(404, "ไม่พบใบเสร็จ")
    return _serialize(row)


def list_order_receipts(conn: sqlite3.Connection, order_code: str) -> list[dict[str, Any]]:
    _order(conn, order_code)
    return [_serialize(row) for row in conn.execute(
        _RECEIPT_SELECT + " WHERE o.order_code = ? ORDER BY r.id DESC", (order_code,)
    ).fetchall()]


def orders_with_receipt_warning(conn: sqlite3.Connection, order_ids: list[int]) -> set[int]:
    """Orders that still hold an active receipt although their status is no longer issuable."""
    if not order_ids:
        return set()
    placeholders = ",".join("?" for _ in order_ids)
    status_marks = ",".join("?" for _ in RECEIPT_ISSUABLE_STATUSES)
    return {row[0] for row in conn.execute(
        "SELECT r.order_id FROM store_receipts r JOIN store_orders o ON o.id = r.order_id "
        f"WHERE r.cancelled_at IS NULL AND r.order_id IN ({placeholders}) AND o.status NOT IN ({status_marks})",
        (*order_ids, *RECEIPT_ISSUABLE_STATUSES),
    )}


def receipt_warning(conn: sqlite3.Connection, order_code: str) -> str | None:
    order = _order(conn, order_code)
    if order["id"] not in orders_with_receipt_warning(conn, [order["id"]]):
        return None
    receipt_no = conn.execute(
        "SELECT receipt_no FROM store_receipts WHERE order_id = ? AND cancelled_at IS NULL", (order["id"],)
    ).fetchone()[0]
    return f"ออเดอร์นี้ไม่ได้อยู่ในสถานะชำระแล้ว แต่ใบเสร็จ {receipt_no} ยังใช้งานอยู่"
