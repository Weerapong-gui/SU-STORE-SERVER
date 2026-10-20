"""HTTP routes for staff management under `/v2/admin/users*`."""
from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from core.database import log_audit, open_db

from . import staff
from .common import StoreError
from .route_helpers import admin_user, json_body, require_superadmin

_USER_ACTION = re.compile(r"/v2/admin/users/(.+)/(reset-password)")
_USER = re.compile(r"/v2/admin/users/(.+)")


def route(handler: Any, method: str, path: str) -> bool:
    """Handle staff management requests and return False for unrelated paths."""
    if path != "/v2/admin/users" and not path.startswith("/v2/admin/users/"):
        return False
    require_superadmin(handler)
    actor = admin_user(handler)
    send = handler.send_json

    if method == "GET" and path == "/v2/admin/users":
        with open_db() as conn:
            send(HTTPStatus.OK, {"users": staff.list_staff(conn)})
        return True

    if method == "POST" and path == "/v2/admin/users":
        payload = json_body(handler)
        username = payload.get("username")
        if not isinstance(username, str):
            raise StoreError(HTTPStatus.BAD_REQUEST, "ชื่อผู้ใช้ใช้ได้เฉพาะ a-z A-Z 0-9 . _ - ไม่เกิน 64 ตัว")
        with open_db() as conn:
            member, password = staff.create_staff(conn, username, actor)
            log_audit(conn, "-", "v2_staff_created", f"{username} by {actor}")
        send(HTTPStatus.OK, {"user": member, "password": password})
        return True

    match = _USER_ACTION.fullmatch(path)
    if method == "POST" and match:
        username = unquote(match.group(1))
        with open_db() as conn:
            password = staff.reset_password(conn, username)
            log_audit(conn, "-", "v2_staff_password_reset", f"{username} by {actor}")
        send(HTTPStatus.OK, {"password": password})
        return True

    match = _USER.fullmatch(path)
    if match:
        username = unquote(match.group(1))
        if method == "PATCH":
            payload = json_body(handler)
            value = payload.get("isSuperadmin")
            if not isinstance(value, bool):
                raise StoreError(HTTPStatus.BAD_REQUEST, "สิทธิ์ผู้ดูแลระบบหลักไม่ถูกต้อง")
            action = "superadmin_on" if value else "superadmin_off"
            with open_db() as conn:
                member = staff.set_superadmin(conn, username, value, actor)
                log_audit(conn, "-", f"v2_staff_{action}", f"{username} by {actor}")
            send(HTTPStatus.OK, member)
            return True
        if method == "DELETE":
            with open_db() as conn:
                staff.delete_staff(conn, username, actor)
                log_audit(conn, "-", "v2_staff_deleted", f"{username} by {actor}")
            send(HTTPStatus.OK, {"ok": True})
            return True
    return False
