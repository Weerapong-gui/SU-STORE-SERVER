"""Category catalog metadata; product availability is independent of visibility."""
from __future__ import annotations

import sqlite3
from typing import Any

from .common import StoreError, as_int, clean_image_url, clean_text, now_iso, slugify, unique_slug


def serialize(row: sqlite3.Row, product_count: int) -> dict[str, Any]:
    return {
        "id": row["id"], "slug": row["slug"], "name": row["name"], "nameEn": row["name_en"],
        "tagline": row["tagline"], "taglineEn": row["tagline_en"], "coverImage": row["cover_image"],
        "visible": bool(row["visible"]), "sortOrder": row["sort_order"], "productCount": product_count,
    }


def _select(public: bool) -> str:
    active = " AND p.status = 'active'" if public else ""
    return (
        "SELECT c.*, (SELECT COUNT(*) FROM store_products p WHERE p.category_id = c.id"
        f"{active}) AS product_count FROM store_categories c"
    )


def list_categories(conn: sqlite3.Connection, *, public: bool) -> list[dict[str, Any]]:
    where = " WHERE c.visible = 1" if public else ""
    rows = conn.execute(_select(public) + where + " ORDER BY c.sort_order, c.id").fetchall()
    return [serialize(row, row["product_count"]) for row in rows]


def get_category(conn: sqlite3.Connection, key: int | str, *, public: bool) -> dict[str, Any]:
    column = "id" if isinstance(key, int) else "slug"
    row = conn.execute(_select(public) + f" WHERE c.{column} = ?", (key,)).fetchone()
    if row is None or (public and not row["visible"]):
        raise StoreError(404, "ไม่พบหมวดหมู่")
    return serialize(row, row["product_count"])


def ensure_exists(conn: sqlite3.Connection, category_id: int | None) -> None:
    if category_id is not None and conn.execute(
        "SELECT 1 FROM store_categories WHERE id = ?", (category_id,)
    ).fetchone() is None:
        raise StoreError(400, "ไม่พบหมวดหมู่ที่เลือก")


def _fields(payload: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, column, label, limit in (
        ("name", "name", "ชื่อหมวดหมู่", 60), ("nameEn", "name_en", "ชื่อหมวดหมู่ภาษาอังกฤษ", 60),
        ("tagline", "tagline", "คำโปรย", 120), ("taglineEn", "tagline_en", "คำโปรยภาษาอังกฤษ", 120),
    ):
        if key in payload:
            fields[column] = clean_text(payload[key], label, required=key == "name", max_len=limit)
    if "coverImage" in payload:
        fields["cover_image"] = clean_image_url(payload["coverImage"], "รูปปก")
    if "visible" in payload:
        if not isinstance(payload["visible"], bool):
            raise StoreError(400, "การแสดงหมวดหมู่ต้องเป็น boolean")
        fields["visible"] = int(payload["visible"])
    return fields


def create_category(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    fields = _fields(payload)
    if "name" not in fields:
        raise StoreError(400, "กรุณากรอกชื่อหมวดหมู่")
    base = clean_text(payload.get("slug") or fields.get("name_en") or fields["name"], "slug")
    fields["slug"] = unique_slug(conn, "store_categories", slugify(base, fallback="category"))
    fields["sort_order"] = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM store_categories").fetchone()[0]
    fields["created_at"] = fields["updated_at"] = now_iso()
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    cursor = conn.execute(f"INSERT INTO store_categories ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    return get_category(conn, int(cursor.lastrowid), public=False)


def update_category(conn: sqlite3.Connection, category_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    get_category(conn, category_id, public=False)
    fields = {**_fields(payload), "updated_at": now_iso()}
    assignments = ", ".join(f"{column} = ?" for column in fields)
    conn.execute(f"UPDATE store_categories SET {assignments} WHERE id = ?", (*fields.values(), category_id))
    return get_category(conn, category_id, public=False)


def delete_category(conn: sqlite3.Connection, category_id: int) -> dict[str, Any]:
    get_category(conn, category_id, public=False)
    cursor = conn.execute("UPDATE store_products SET category_id = NULL WHERE category_id = ?", (category_id,))
    conn.execute("DELETE FROM store_categories WHERE id = ?", (category_id,))
    return {"deleted": True, "unassigned": cursor.rowcount}


def reorder_categories(conn: sqlite3.Connection, ids: list[int]) -> list[dict[str, Any]]:
    if not isinstance(ids, list):
        raise StoreError(400, "ลำดับหมวดหมู่ไม่ถูกต้อง")
    checked = [as_int(value, "หมวดหมู่", minimum=1) for value in ids]
    existing = {row[0] for row in conn.execute("SELECT id FROM store_categories")}
    if len(checked) != len(existing) or set(checked) != existing:
        raise StoreError(400, "ลำดับหมวดหมู่ต้องมีทุกหมวดหมู่")
    now = now_iso()
    conn.executemany("UPDATE store_categories SET sort_order = ?, updated_at = ? WHERE id = ?",
                     [(position, now, category_id) for position, category_id in enumerate(checked)])
    return list_categories(conn, public=False)
