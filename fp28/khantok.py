"""Khantok ticket quota, claims and pickup warnings."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import config
from .config import KHANTOK_STUDENT_CODE_PREFIX
from .timeutil import TZ_BANGKOK, now_iso, parse_stored_datetime


def khantok_claim_warning(received_at: str | None, status: str | None = None,
                          now: datetime | None = None) -> bool:
    """True เมื่อออเดอร์รับของไปแล้วในวันไทยที่เก่ากว่าวันนี้ หรือรับของไปแล้ว
    (status == 'received') แต่ไม่มี received_at ที่ใช้พิสูจน์ได้ว่าเป็นวันนี้

    ออเดอร์ที่รับของไปก่อนจะมีการรับบัตรแยก อาจได้บัตรขันโตกไปพร้อมของแล้ว
    claim station จึงเตือนก่อนแจกใบใหม่ ส่วนคนที่มารับวันเดียวกันต้องไม่เตือน
    เพราะ flow ปกติคือ staff กดรับของแล้วกดรับบัตรต่อห่างกันไม่กี่นาที

    รับของแล้ว (admin กดสถานะ 'received' ตรงๆ ไม่ผ่าน claim station) แต่ timestamp
    หาย/ว่าง/parse ไม่ได้ ก็ยังต้องเตือน เพราะพิสูจน์ไม่ได้ว่าเป็นการรับของวันนี้
    """
    received = parse_stored_datetime(received_at)
    if status == "received" and received is None:
        return True
    if received is None:
        return False
    current = now or datetime.now(TZ_BANGKOK)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ_BANGKOK)
    return received.astimezone(TZ_BANGKOK).date() < current.astimezone(TZ_BANGKOK).date()


def reserve_khantok_ticket(
    connection: sqlite3.Connection, order_id: int, student_code: str
) -> tuple[bool, str | None, bool, int | None]:
    existing_claim = connection.execute(
        "SELECT claimed_at, ticket_value FROM khantok_ticket_claims WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if existing_claim is not None:
        return True, str(existing_claim["claimed_at"]), False, int(existing_claim["ticket_value"])

    code = (student_code or "").strip()
    if not code.startswith(KHANTOK_STUDENT_CODE_PREFIX):
        return False, None, False, None

    # Only a still-live order should block a re-claim. A ticketed order that was later
    # cancelled/rejected/refunded must NOT permanently deny the student a ticket on a new
    # order. (Read-only guard — does not mutate the dead order's data.)
    duplicate = connection.execute(
        "SELECT 1 FROM orders WHERE student_code = ? AND khantok_ticket = 1 AND internal_id != ? "
        "AND status NOT IN ('cancelled', 'rejected', 'refund', 'refunded')",
        (code, order_id),
    ).fetchone()
    if duplicate is not None:
        return False, None, True, None

    count_100 = int(connection.execute(
        "SELECT COUNT(*) AS c FROM khantok_ticket_claims WHERE ticket_value = 100"
    ).fetchone()["c"])
    if count_100 < config.KHANTOK_QUOTA_100:
        ticket_value = 100
    else:
        count_50 = int(connection.execute(
            "SELECT COUNT(*) AS c FROM khantok_ticket_claims WHERE ticket_value = 50"
        ).fetchone()["c"])
        if count_50 < config.KHANTOK_QUOTA_50:
            ticket_value = 50
        else:
            return False, None, False, None

    claimed_at = now_iso()
    connection.execute(
        "INSERT INTO khantok_ticket_claims (order_id, claimed_at, ticket_value) VALUES (?, ?, ?)",
        (order_id, claimed_at, ticket_value),
    )
    return True, claimed_at, False, ticket_value
