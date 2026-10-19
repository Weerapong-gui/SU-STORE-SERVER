"""Request helpers shared by the `/v2/*` route modules (`api.py`, `receipt_routes.py`)."""
from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fp28.database import open_db

from .common import StoreError

MAX_JSON_BYTES = 512 * 1024


def is_admin(handler: Any) -> bool:
    if handler.has_global_authorization():
        return True
    return handler.has_claim_station_authorization()


def admin_user(handler: Any) -> str:
    return handler.get_claim_station_user() or "token"


def is_superadmin(handler: Any) -> bool:
    if handler.has_global_authorization():
        return True
    with open_db() as conn:
        row = conn.execute(
            "SELECT is_superadmin FROM admin_users WHERE username = ?", (admin_user(handler),)
        ).fetchone()
    return bool(row and row["is_superadmin"])


def require_superadmin(handler: Any) -> None:
    if not is_superadmin(handler):
        raise StoreError(HTTPStatus.FORBIDDEN, "เฉพาะผู้ดูแลระบบหลัก")


def content_length(handler: Any) -> int:
    try:
        return int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise StoreError(HTTPStatus.BAD_REQUEST, "ข้อมูลไม่ถูกต้อง") from None


def json_body(handler: Any) -> dict[str, Any]:
    # Refuse oversized bodies before read_json pulls them into memory.
    if content_length(handler) > MAX_JSON_BYTES:
        raise StoreError(HTTPStatus.BAD_REQUEST, "ข้อมูลใหญ่เกินไป")
    payload = handler.read_json()
    if not isinstance(payload, dict):
        raise StoreError(HTTPStatus.BAD_REQUEST, "ข้อมูลไม่ถูกต้อง")
    return payload
