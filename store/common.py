"""Shared validation and catalog helpers for SU STORE v2."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

TZ_BANGKOK = timezone(timedelta(hours=7))
MAX_FIELD_LENGTH = 500
_IMAGE_URL_RE = re.compile(r"/product-images/[A-Za-z0-9._-]+")


class StoreError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso() -> str:
    return datetime.now(TZ_BANGKOK).replace(microsecond=0).isoformat()


def clean_text(value: Any, field: str, *, required: bool = False, max_len: int = MAX_FIELD_LENGTH) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise StoreError(400, f"{field} ต้องเป็นข้อความ")
    value = value.strip()
    if required and not value:
        raise StoreError(400, f"กรุณากรอก{field}")
    if len(value) > max_len:
        raise StoreError(400, f"{field} ยาวเกิน {max_len} ตัวอักษร")
    return value


def as_int(value: Any, field: str, *, minimum: int = 0, allow_none: bool = False) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise StoreError(400, f"กรุณากรอก{field}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError(400, f"{field} ต้องเป็นจำนวนเต็ม")
    if value < minimum:
        raise StoreError(400, f"{field} ต้องไม่น้อยกว่า {minimum}")
    return value


def slugify(name: str, fallback: str = "product") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48] or fallback


def unique_slug(
    connection: sqlite3.Connection,
    table: Literal["store_products", "store_categories"],
    base: str,
    exclude_id: int | None = None,
) -> str:
    # Table names cannot be bound as SQL parameters; only these catalog tables are allowed.
    if table not in ("store_products", "store_categories"):
        raise ValueError("Unsupported slug table")
    slug, n = base, 2
    while True:
        row = connection.execute(f"SELECT id FROM {table} WHERE slug = ?", (slug,)).fetchone()
        if row is None or row[0] == exclude_id:
            return slug
        slug, n = f"{base}-{n}", n + 1


def clean_image_url(value: Any, field: str) -> str:
    value = clean_text(value, field, max_len=200)
    if value and not _IMAGE_URL_RE.fullmatch(value):
        raise StoreError(400, f"{field}ต้องเป็นรูปที่อัปโหลดผ่านหลังร้าน")
    return value
