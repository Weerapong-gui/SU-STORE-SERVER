"""Site settings: open/closed switch, schedule and sale phases."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any

from . import config
from .config import BYPASS_TOKEN
from .timeutil import TZ_BANGKOK, now_iso


def get_schedule_status(schedule_enabled: bool, warning_message: str) -> dict[str, Any]:
    """Compute schedule open/closed/warning state from current Bangkok time."""
    if not schedule_enabled:
        return {"scheduleClosed": False, "scheduleWarning": False, "scheduleWarningMessage": warning_message}
    now = datetime.now(TZ_BANGKOK)
    total_min = now.hour * 60 + now.minute
    open_min = 6 * 60       # 06:00
    close_min = 23 * 60     # 23:00 (end of 22:59)
    warn_min = close_min - 10  # 22:50
    schedule_closed = total_min < open_min or total_min >= close_min
    schedule_warning = not schedule_closed and total_min >= warn_min
    return {
        "scheduleClosed": schedule_closed,
        "scheduleWarning": schedule_warning,
        "scheduleWarningMessage": warning_message,
    }


def get_current_phase(connection: sqlite3.Connection | None = None) -> int:
    if connection is not None:
        row = connection.execute(
            "SELECT value FROM site_settings WHERE key = 'phase_override'"
        ).fetchone()
        if row is not None and row["value"] not in ("", None):
            try:
                return int(row["value"])
            except (ValueError, TypeError):
                pass
        config_row = connection.execute(
            "SELECT value FROM site_settings WHERE key = 'phase_configs'"
        ).fetchone()
        if config_row is not None and config_row["value"]:
            try:
                configs = json.loads(config_row["value"])
                today = datetime.now(TZ_BANGKOK).date()
                for phase_str, info in configs.items():
                    s, e = info.get("start", ""), info.get("end", "")
                    if s and e and date.fromisoformat(s) <= today <= date.fromisoformat(e):
                        return int(phase_str)
            except (json.JSONDecodeError, ValueError, KeyError):
                pass
    now = datetime.now(TZ_BANGKOK)
    m, d = now.month, now.day
    if m == 5 and 18 <= d <= 23:
        return 1
    if m == 5 and 25 <= d <= 30:
        return 2
    if m == 6 and 1 <= d <= 7:
        return 3
    return 0


def get_site_settings(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute("SELECT key, value FROM site_settings").fetchall()
    settings: dict[str, str] = {row["key"]: row["value"] for row in rows}
    phase_override_raw = settings.get("phase_override", "")
    phase_override = int(phase_override_raw) if phase_override_raw not in ("", None) else None
    phase_configs_raw = settings.get("phase_configs", "")
    try:
        phase_configs: dict = json.loads(phase_configs_raw) if phase_configs_raw else {}
    except (json.JSONDecodeError, TypeError):
        phase_configs = {}
    if not phase_configs:
        phase_configs = {
            "1": {"start": "2026-05-18", "end": "2026-05-23"},
            "2": {"start": "2026-05-25", "end": "2026-05-30"},
            "3": {"start": "2026-06-01", "end": "2026-06-07"},
        }
    max_phases = max((int(k) for k in phase_configs), default=3)
    return {
        "announcementBanner": settings.get("announcement_banner", ""),
        "announcementBannerEnabled": settings.get("announcement_banner_enabled", "0") == "1",
        "storeOpen": settings.get("store_open", "1") == "1",
        "siteClosed": settings.get("site_closed", "0") == "1",
        "scheduleEnabled": settings.get("schedule_enabled", "0") == "1",
        "scheduleWarningMessage": settings.get("schedule_warning_message", "เว็บกำลังจะปิด กรุณาทำรายการให้เสร็จก่อนเวลา 22:59"),
        "orderDeadline": settings.get("order_deadline", ""),
        "phaseOverride": phase_override,
        "currentPhase": get_current_phase(connection),
        "phaseConfigs": phase_configs,
        "maxPhases": max_phases,
        "khantokQuota100": int(settings.get("khantok_quota_100") or config.KHANTOK_QUOTA_100),
        "khantokQuota50": int(settings.get("khantok_quota_50") or config.KHANTOK_QUOTA_50),
        "beRightBackDates": settings.get("be_right_back_dates", "2026-05-24,2026-05-31,2026-06-07"),
        "beRightBackActive": settings.get("be_right_back_active", "0") == "1",
        "bypassToken": BYPASS_TOKEN,
    }


def upsert_site_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO site_settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )
