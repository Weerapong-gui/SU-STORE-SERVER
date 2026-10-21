"""Tests for the isolated demo catalog used by local development."""
from __future__ import annotations

import sqlite3

from core import config, database
from store import categories, db, home
from store.demo_seed import seed_demo


def _database(path: str = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE IF NOT EXISTS site_settings "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    db.ensure_store_db(connection)
    return connection


def test_seed_creates_catalog_and_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setattr(config, "SLIPS_DIR", tmp_path / "slips")
    database.ensure_db()
    connection = _database(str(config.DB_PATH))

    assert seed_demo(connection) is True
    assert connection.execute("SELECT COUNT(*) FROM store_categories").fetchone()[0] >= 2
    products = db.list_products(connection, public=False)
    options = [product for product in products if product["optionKind"] == "options"]
    assert any({variant["size"] for variant in p["variants"]} >= {"S", "M"} and
               {variant["color"] for variant in p["variants"]} >= {"กรมท่า", "ขาว"} for p in options)
    designs = [product for product in products if product["optionKind"] == "designs"]
    assert any(len(product["variants"]) >= 3 for product in designs)
    assert home.get_home_admin(connection)["published"]["blocks"]
    statuses = {row[0] for row in connection.execute("SELECT DISTINCT status FROM store_orders")}
    assert set(db.ORDER_STATUSES) <= statuses
    assert db.get_settings(connection)["payment"]["accountNumber"]


def test_seed_is_noop_when_products_exist():
    connection = _database()
    category = categories.create_category(connection, {"name": "มีสินค้าแล้ว"})
    db.create_product(connection, {"name": "สินค้าที่มีอยู่", "categoryId": category["id"]})
    before = connection.execute("SELECT COUNT(*) FROM store_orders").fetchone()[0]

    assert seed_demo(connection) is False
    assert connection.execute("SELECT COUNT(*) FROM store_orders").fetchone()[0] == before


def test_seed_only_through_business_functions(tmp_path):
    path = str(tmp_path / "seed.db")
    connection = _database(path)
    assert seed_demo(connection) is True
    connection.commit()
    connection.close()

    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    assert db.list_products(reopened, public=True)
    reopened.close()
