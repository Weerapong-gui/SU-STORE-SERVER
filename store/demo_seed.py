"""Create a disposable bilingual catalog for local development.

If seeding stops partway through, remove the demo database with ``./dev.sh reset``.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import categories, db, home


def _create_product(connection: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Create and activate one demo product through the catalog business logic."""
    return db.create_product(connection, {**payload, "status": "active"})


def seed_demo(connection: sqlite3.Connection) -> bool:
    """Seed a clean development database; use ``./dev.sh reset`` after a partial failure."""
    if connection.execute("SELECT 1 FROM store_products LIMIT 1").fetchone():
        return False

    categories_rows = [
        categories.create_category(connection, {
            "name": "เสื้อผ้าและของใช้", "nameEn": "Apparel & goods",
            "tagline": "ของใช้ประจำวัน", "taglineEn": "Everyday essentials",
        }),
        categories.create_category(connection, {
            "name": "ลายพิเศษ", "nameEn": "Special designs",
            "tagline": "เลือกแบบที่ชอบ", "taglineEn": "Choose your favorite",
        }),
    ]
    options_product = _create_product(connection, {
        "name": "เสื้อยืดมหาวิทยาลัย", "nameEn": "University T-shirt",
        "description": "เสื้อยืดตัวอย่างสำหรับทดลองสั่งซื้อ / A sample shirt for checkout testing",
        "categoryId": categories_rows[0]["id"], "optionKind": "options",
        "variants": [
            {"size": size, "color": color, "price": 250, "stock": None}
            for size in ("S", "M", "L") for color in ("กรมท่า", "ขาว")
        ],
    })
    _create_product(connection, {
        "name": "กระเป๋าผ้าลายพิเศษ", "nameEn": "Special canvas bag",
        "description": "เลือกได้สามลาย / Three designs to choose from",
        "categoryId": categories_rows[1]["id"], "optionKind": "designs",
        "variants": [
            {"designCode": code, "designName": name, "designNameEn": name_en, "price": 180, "stock": None}
            for code, name, name_en in (
                ("SUN", "พระอาทิตย์", "Sun"), ("MOON", "พระจันทร์", "Moon"),
                ("STAR", "ดวงดาว", "Star"),
            )
        ],
    })

    home.save_home_draft(connection, {
        "accent": "#0055aa", "radius": 18, "font": "sarabun",
        "blocks": [
            {"id": "demo-intro", "type": "hero", "hidden": False, "autoplay": False,
             "slides": [{"image": "", "eyebrow": "SU STORE", "title": "ร้านค้าตัวอย่าง",
                         "subtitle": "สินค้าสำหรับทดลองระบบ / Sample catalog for development",
                         "buttonText": "", "buttonHref": ""}]},
            {"id": "demo-catalog", "type": "allProducts", "hidden": False,
             "title": "สินค้าตัวอย่าง / Demo products"},
        ],
    })
    home.publish_home(connection)
    db.update_settings(connection, {"payment": {
        "bankName": "ธนาคารตัวอย่าง", "accountNumber": "000-0-00000-0",
        "accountName": "บัญชีตัวอย่าง",
    }})
    connection.commit()

    first_variant = options_product["variants"][0]["id"]
    for status in db.ORDER_STATUSES:
        order = db.create_order(connection, {
            "items": [{"variantId": first_variant, "quantity": 1}],
            "customer": {"name": "ผู้ใช้ตัวอย่าง", "phone": "0812345678", "email": f"demo-{status}@example.invalid"},
        })
        if status != "pending_payment":
            db.update_order(connection, order["orderCode"], {"status": status})

    connection.commit()
    return True
