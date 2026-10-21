"""Bangkok-time helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ_BANGKOK = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ_BANGKOK).replace(microsecond=0).isoformat()


