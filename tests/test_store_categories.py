"""Additive catalog migrations preserve existing order snapshots."""
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.store import api, categories, db

from .test_store_v2 import CUSTOMER, _call, _conn, _product, server  # noqa: F401


def test_migration_adds_columns_and_keeps_old_rows() -> None:
    conn = _conn()
    product = _product(conn)
    db.create_order(conn, {"items": [{"variantId": product["variants"][0]["id"], "quantity": 1}], "customer": CUSTOMER})
    db.ensure_store_db(conn)
    row = conn.execute("SELECT product_name, variant_label, product_name_en, variant_label_en FROM store_order_items").fetchone()
    assert row["product_name"] == "SU Tote Bag"
    assert row["product_name_en"] is None
    assert row["variant_label_en"] is None
    cols = {r[1] for r in conn.execute("PRAGMA table_info(store_products)")}
    assert {"name_en", "category_id", "option_kind"} <= cols
    assert db.get_product(conn, product["id"], public=True)["optionKind"] == "options"
    assert db.get_product(conn, product["id"], public=False)["optionKind"] == "options"
    variant = conn.execute("SELECT design_name, design_name_en, design_code, image FROM store_variants").fetchone()
    assert tuple(variant) == ("", "", "", "")
    conn.execute("INSERT INTO store_categories (slug, name, created_at, updated_at) VALUES ('bags', 'กระเป๋า', 'now', 'now')")
    category = conn.execute("SELECT * FROM store_categories").fetchone()
    assert dict(category) == {
        "id": 1, "slug": "bags", "name": "กระเป๋า", "name_en": "", "tagline": "",
        "tagline_en": "", "cover_image": "", "visible": 1, "sort_order": 0,
        "created_at": "now", "updated_at": "now",
    }
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(store_products)")}
    assert "idx_store_products_category" in indexes


def test_migration_on_legacy_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS store_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            images_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft',
            buyer_fields_json TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS store_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES store_products(id),
            size TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '',
            price INTEGER NOT NULL,
            stock INTEGER,
            sku TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_store_variants_product ON store_variants(product_id);
        CREATE TABLE IF NOT EXISTS store_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            customer_json TEXT NOT NULL,
            total_amount INTEGER NOT NULL,
            access_token TEXT NOT NULL,
            admin_note TEXT NOT NULL DEFAULT '',
            slip_original_name TEXT,
            slip_stored_name TEXT,
            slip_storage_path TEXT,
            slip_mime_type TEXT,
            slip_size INTEGER,
            slip_uploaded_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_store_orders_status ON store_orders(status);
        CREATE INDEX IF NOT EXISTS idx_store_orders_created ON store_orders(created_at);
        CREATE TABLE IF NOT EXISTS store_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES store_orders(id),
            variant_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            variant_label TEXT NOT NULL,
            unit_price INTEGER NOT NULL,
            quantity INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO store_order_items "
        "(id, order_id, variant_id, product_id, product_name, variant_label, unit_price, quantity) "
        "VALUES (1, 2, 3, 4, 'Original bag', 'M / Blue', 150, 2)"
    )
    original = dict(conn.execute("SELECT * FROM store_order_items").fetchone())
    db.ensure_store_db(conn)
    db.ensure_store_db(conn)
    migrated = dict(conn.execute("SELECT * FROM store_order_items").fetchone())
    assert migrated == {**original, "product_name_en": None, "variant_label_en": None}
    columns = {r[1]: r for r in conn.execute("PRAGMA table_info(store_order_items)")}
    for name in ("product_name_en", "variant_label_en"):
        assert columns[name][2] == "TEXT"
        assert columns[name][3] == 0
        assert columns[name][4] is None


def test_create_category_slug_from_english_name() -> None:
    conn = _conn()
    assert categories.create_category(conn, {"name": "พวงกุญแจ", "nameEn": "Keychain"})["slug"] == "keychain"
    assert categories.create_category(conn, {"name": "พวงกุญแจ", "nameEn": "Keychain"})["slug"] == "keychain-2"
    assert categories.create_category(conn, {"name": "พวงกุญแจ"})["slug"] == "category"


def test_hidden_category_is_404_publicly_but_products_still_listed() -> None:
    conn = _conn()
    category = categories.create_category(conn, {"name": "Bags"})
    product = _category_product(conn, category_id=category["id"])
    categories.update_category(conn, category["id"], {"visible": False})
    with pytest.raises(db.StoreError) as error:
        categories.get_category(conn, category["slug"], public=True)
    assert (error.value.status, error.value.message) == (404, "ไม่พบหมวดหมู่")
    assert categories.list_categories(conn, public=True) == []
    assert categories.list_categories(conn, public=False)[0]["visible"] is False
    assert db.list_products(conn, public=True)[0]["id"] == product["id"]
    assert db.create_order(conn, {"items": [{"variantId": product["variants"][0]["id"], "quantity": 1}], "customer": CUSTOMER})


def test_delete_category_unassigns_products() -> None:
    conn = _conn()
    category = categories.create_category(conn, {"name": "Bags"})
    product = _category_product(conn, category_id=category["id"])
    assert categories.delete_category(conn, category["id"]) == {"deleted": True, "unassigned": 1}
    assert db.get_product(conn, product["id"], public=False)["categoryId"] is None


def test_reorder_requires_every_id() -> None:
    conn = _conn()
    first = categories.create_category(conn, {"name": "First"})
    second = categories.create_category(conn, {"name": "Second"})
    with pytest.raises(db.StoreError) as error:
        categories.reorder_categories(conn, [first["id"]])
    assert error.value.status == 400
    assert [c["id"] for c in categories.reorder_categories(conn, [second["id"], first["id"]])] == [second["id"], first["id"]]


def test_cover_image_must_be_uploaded_path() -> None:
    with pytest.raises(db.StoreError) as error:
        categories.create_category(_conn(), {"name": "Bags", "coverImage": "https://evil/x.png"})
    assert error.value.status == 400


def test_category_products_only_active() -> None:
    conn = _conn()
    category = categories.create_category(conn, {"name": "Bags"})
    active = _category_product(conn, category_id=category["id"])
    _category_product(conn, category_id=category["id"], status="draft")
    _product(conn)
    category = categories.get_category(conn, category["slug"], public=True)
    products = db.list_products(conn, public=True, category_id=category["id"])
    assert [p["id"] for p in products] == [active["id"]]
    assert category["productCount"] == 1
    assert categories.get_category(conn, category["id"], public=False)["productCount"] == 2


@pytest.mark.parametrize("value", [999, True, "1", -1, 1.5, ""])
def test_product_category_rejects_invalid_id(value: object) -> None:
    conn = _conn()
    product = _product(conn)
    with pytest.raises(db.StoreError) as error:
        _category_product(conn, category_id=value)
    assert error.value.status == 400
    with pytest.raises(db.StoreError):
        db.update_product(conn, product["id"], {"categoryId": value})
    if value == 999:
        assert error.value.message == "ไม่พบหมวดหมู่ที่เลือก"


def test_category_update_validation_and_immutable_slug() -> None:
    conn = _conn()
    category = categories.create_category(conn, {"name": "Bags", "coverImage": "/product-images/bag.png"})
    updated = categories.update_category(conn, category["id"], {"slug": "changed", "nameEn": "Bag", "tagline": "Hello"})
    assert updated["slug"] == "bags" and updated["nameEn"] == "Bag" and updated["tagline"] == "Hello"
    for payload in ({"name": ""}, {"name": "x" * 61}, {"nameEn": "x" * 61}, {"tagline": "x" * 121}, {"taglineEn": "x" * 121}, {"visible": 1}):
        with pytest.raises(db.StoreError):
            categories.update_category(conn, category["id"], payload)


def test_categories_http_round_trip(server: str) -> None:  # noqa: F811
    admin = {"Authorization": "Bearer test-token"}
    assert _call(server, "POST", "/v2/admin/images")[0] == 401
    assert _call(server, "POST", "/v2/admin/categories", {"name": "Denied"})[0] == 401
    status, body = _call(server, "POST", "/v2/admin/categories", {"name": "Visible"}, admin)
    assert status == 201, body
    visible = json.loads(body)
    status, body = _call(server, "POST", "/v2/admin/categories", {"name": "Hidden", "visible": False}, admin)
    assert status == 201, body
    hidden = json.loads(body)
    assert json.loads(_call(server, "GET", "/v2/categories")[1])["categories"] == [visible]
    assert _call(server, "GET", f"/v2/categories/{hidden['slug']}")[0] == 404
    assert json.loads(_call(server, "GET", f"/v2/categories/{visible['slug']}")[1]) == {"category": visible, "products": []}
    assert _call(server, "PUT", "/v2/admin/categories/order", {"ids": [hidden["id"], visible["id"]]}, admin)[0] == 200
    assert _call(server, "PUT", f"/v2/admin/categories/{visible['id']}", {"name": "Updated"}, admin)[0] == 200
    assert _call(server, "GET", f"/v2/admin/categories/{visible['id']}", headers=admin)[0] == 200
    assert _call(server, "DELETE", f"/v2/admin/categories/{visible['id']}", headers=admin)[0] == 200
    with sqlite3.connect(api.config.DB_PATH) as conn:
        events = conn.execute("SELECT event, detail FROM order_audit_log WHERE event LIKE 'v2_category_%' ORDER BY id").fetchall()
    assert [event for event, _ in events] == [
        "v2_category_created", "v2_category_created", "v2_category_reordered", "v2_category_updated", "v2_category_deleted",
    ]
    assert all(detail.endswith(" by token") for _, detail in events)
    assert events[-1][1] == "visible by token"


def _category_product(conn: sqlite3.Connection, *, category_id: object, status: str = "active") -> dict:
    return db.create_product(conn, {"name": "Bag", "categoryId": category_id, "status": status,
                                    "variants": [{"price": 150, "stock": 5}]})


@pytest.mark.parametrize("path,prefix", [("/v2/admin/images", "admin-"), ("/v2/admin/home/images", "home-")])
def test_admin_image_upload_saves_uploaded_content(
    path: str, prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api.config, "PRODUCT_IMAGES_DIR", tmp_path)
    content = b"\x89PNG fake"
    raw = (
        b'--boundary\r\nContent-Disposition: form-data; name="image"; filename="cover.png"\r\n'
        b'Content-Type: image/png\r\n\r\n' + content + b'\r\n--boundary--\r\n'
    )
    responses: list[tuple[int, dict[str, Any]]] = []
    handler = SimpleNamespace(
        path=path, headers={"Content-Type": "multipart/form-data; boundary=boundary", "Content-Length": str(len(raw))},
        rfile=io.BytesIO(raw), has_global_authorization=lambda: True,
        send_json=lambda status, body: responses.append((status, body)),
    )
    api.handle(handler, "POST")
    status, body = responses[0]
    assert status == 200
    assert body["url"].startswith("/product-images/" + prefix)
    assert (tmp_path / body["url"].split("/")[-1]).read_bytes() == content
