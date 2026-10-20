"""Live "visitors online" count, fed by the store's /site-status heartbeat.

A visitor is keyed by the anonymous `su_vid` cookie the Next.js middleware sets, so
students sharing one campus/mobile IP are counted separately. Requests without the
cookie fall back to the client IP.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time

ACTIVE_SECONDS = 120  # 2 minutes
# Random ids are cheap to forge: cap the registry so nobody can grow it without bound.
MAX_TRACKED = 20_000
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_last_seen: dict[str, float] = {}
_lock = threading.Lock()


def visitor_key(visitor_id: str, ip: str) -> str | None:
    """The registry key for a heartbeat, or None if neither value is usable."""
    visitor_id = visitor_id.strip().lower()
    if _UUID.fullmatch(visitor_id):
        return f"vid:{visitor_id}"
    try:
        return f"ip:{ipaddress.ip_address(ip.strip())}"
    except ValueError:
        return None


def record_visit(key: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        if key not in _last_seen and len(_last_seen) >= MAX_TRACKED:
            _drop_expired(now)
            if len(_last_seen) >= MAX_TRACKED:
                return
        _last_seen[key] = now


def active_count(now: float | None = None) -> int:
    cutoff = (time.time() if now is None else now) - ACTIVE_SECONDS
    with _lock:
        return sum(1 for seen in _last_seen.values() if seen >= cutoff)


def _drop_expired(now: float) -> None:
    cutoff = now - ACTIVE_SECONDS
    for key in [key for key, seen in _last_seen.items() if seen < cutoff]:
        del _last_seen[key]
