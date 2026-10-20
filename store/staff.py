"""Business logic for managing SU STORE staff accounts."""
from __future__ import annotations

import re
import secrets
import sqlite3
from typing import TypedDict

from core.auth import compute_claim_token, compute_super_token
from core.timeutil import now_iso

from .common import StoreError

_USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
_USERNAME_ERROR = "ชื่อผู้ใช้ใช้ได้เฉพาะ a-z A-Z 0-9 . _ - ไม่เกิน 64 ตัว"


class StaffMember(TypedDict):
    username: str
    isSuperadmin: bool
    createdBy: str
    createdAt: str


def _serialize(row: sqlite3.Row) -> StaffMember:
    return {
        "username": row["username"],
        "isSuperadmin": bool(row["is_superadmin"]),
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
    }


def _new_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(12))


def _user(conn: sqlite3.Connection, username: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT username, is_superadmin, created_by, created_at FROM admin_users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        raise StoreError(404, "ไม่พบผู้ใช้นี้")
    return row


def list_staff(conn: sqlite3.Connection) -> list[StaffMember]:
    rows = conn.execute(
        "SELECT username, is_superadmin, created_by, created_at FROM admin_users "
        "ORDER BY is_superadmin DESC, created_at ASC"
    ).fetchall()
    return [_serialize(row) for row in rows]


def create_staff(conn: sqlite3.Connection, username: str, created_by: str) -> tuple[StaffMember, str]:
    if not _USERNAME.fullmatch(username):
        raise StoreError(400, _USERNAME_ERROR)
    password = _new_password()
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM admin_users WHERE username = ?", (username,)).fetchone():
            raise StoreError(409, "ชื่อผู้ใช้นี้มีอยู่แล้ว")
        created_at = now_iso()
        conn.execute(
            "INSERT INTO admin_users "
            "(username, claim_token, super_token, is_superadmin, created_by, created_at) "
            "VALUES (?, ?, NULL, 0, ?, ?)",
            (username, compute_claim_token(username, password), created_by, created_at),
        )
        member = _serialize(_user(conn, username))
        conn.commit()
        return member, password
    except Exception:
        conn.rollback()
        raise


def reset_password(conn: sqlite3.Connection, username: str) -> str:
    password = _new_password()
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _user(conn, username)
        super_token = compute_super_token(username, password) if row["is_superadmin"] else None
        conn.execute(
            "UPDATE admin_users SET claim_token = ?, super_token = ? WHERE username = ?",
            (compute_claim_token(username, password), super_token, username),
        )
        conn.commit()
        return password
    except Exception:
        conn.rollback()
        raise


def set_superadmin(conn: sqlite3.Connection, username: str, value: bool, actor: str) -> StaffMember:
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _user(conn, username)
        if not value and row["is_superadmin"]:
            count = conn.execute("SELECT COUNT(*) FROM admin_users WHERE is_superadmin = 1").fetchone()[0]
            if count <= 1:
                raise StoreError(409, "ต้องเหลือผู้ดูแลระบบหลักอย่างน้อย 1 คน")
        conn.execute("UPDATE admin_users SET is_superadmin = ?, super_token = NULL WHERE username = ?",
                     (int(value), username))
        member = _serialize(_user(conn, username))
        conn.commit()
        return member
    except Exception:
        conn.rollback()
        raise


def delete_staff(conn: sqlite3.Connection, username: str, actor: str) -> None:
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _user(conn, username)
        if username == actor:
            raise StoreError(403, "ลบบัญชีตัวเองไม่ได้")
        if row["is_superadmin"]:
            count = conn.execute("SELECT COUNT(*) FROM admin_users WHERE is_superadmin = 1").fetchone()[0]
            if count <= 1:
                raise StoreError(409, "ต้องเหลือผู้ดูแลระบบหลักอย่างน้อย 1 คน")
        conn.execute("DELETE FROM admin_users WHERE username = ?", (username,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
