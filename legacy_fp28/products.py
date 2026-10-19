"""FP28 (v1) products."""
from __future__ import annotations

import sqlite3
from typing import Any

from core.timeutil import now_iso


def serialize_product(row: sqlite3.Row) -> dict[str, Any]:
    image = row["image_path"] or "/images/POLP_post/Artboard 4.png"
    return {
        "slug": row["slug"],
        "name": row["name"],
        "shortName": row["short_name"],
        "tagline": row["tagline"],
        "description": row["description"],
        "price": row["price"],
        "category": row["category"],
        "requiresSize": bool(row["requires_size"]),
        "requiresSchool": bool(row["requires_school"]),
        "available": bool(row["available"]),
        "image": image,
        "images": [image, image],
        "sortOrder": row["sort_order"],
    }


def get_all_products(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM products ORDER BY sort_order ASC, id ASC"
    ).fetchall()
    return [serialize_product(row) for row in rows]


def get_product(connection: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM products WHERE slug = ?", (slug,)).fetchone()
    return serialize_product(row) if row else None


def update_product(connection: sqlite3.Connection, slug: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM products WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    connection.execute(
        """
        UPDATE products
        SET name = ?, short_name = ?, tagline = ?, description = ?, price = ?, updated_at = ?
        WHERE slug = ?
        """,
        (
            str(payload.get("name", row["name"])).strip(),
            str(payload.get("shortName", row["short_name"])).strip(),
            str(payload.get("tagline", row["tagline"])).strip(),
            str(payload.get("description", row["description"])).strip(),
            max(0, int(payload.get("price", row["price"]))),
            now_iso(),
            slug,
        ),
    )
    return get_product(connection, slug)


def toggle_product_available(connection: sqlite3.Connection, slug: str, available: bool) -> dict[str, Any] | None:
    row = connection.execute("SELECT id FROM products WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    connection.execute(
        "UPDATE products SET available = ?, updated_at = ? WHERE slug = ?",
        (1 if available else 0, now_iso(), slug),
    )
    return get_product(connection, slug)


def update_product_image(connection: sqlite3.Connection, slug: str, image_path: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT id FROM products WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    connection.execute(
        "UPDATE products SET image_path = ?, updated_at = ? WHERE slug = ?",
        (image_path, now_iso(), slug),
    )
    return get_product(connection, slug)
