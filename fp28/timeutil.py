"""Bangkok-time helpers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

TZ_BANGKOK = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ_BANGKOK).replace(microsecond=0).isoformat()


def parse_stored_datetime(value: str | None) -> datetime | None:
    """แปลงค่า timestamp ในคอลัมน์ให้เป็น datetime ที่มี timezone

    แถวใหม่เขียนด้วย now_iso() จึงมี offset +07:00 ติดมา ส่วนแถวเก่าเก็บเป็น UTC
    (บางแถวลงท้าย 'Z' บางแถวไม่มี suffix) ค่าที่ไม่มี offset จึงถือเป็น UTC
    ตรงกับที่ทุกหน้าจอแสดงผลกันอยู่ คืน None เมื่อ parse ไม่ได้ ไม่ raise
    """
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
