"""Regression checks for removing the legacy FP28 API from the server."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from core import config, database

from .test_store_v2 import _call, server  # noqa: F401 (import pytest fixture)

SERVER = Path(__file__).resolve().parents[1]


def test_fp28_paths_are_gone(server):  # noqa: F811 (pytest fixture argument shadows imported fixture)
    headers = {"Authorization": "Bearer test-token"}
    paths = [
        ("GET", "/admin"),
        ("GET", "/orders"),
        ("GET", "/claim-station"),
        ("GET", "/check-order?studentCode=6931"),
        ("POST", "/orders"),
        ("GET", "/"),
    ]
    for method, path in paths:
        status, body = _call(server, method, path, headers=headers)
        assert status == 404, (method, path, status, body)
        assert body == b'{"message": "not found"}', (method, path, body)


def test_ensure_db_on_copied_production_db(tmp_path, monkeypatch):
    path = tmp_path / "copied-production.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "SLIPS_DIR", tmp_path / "slips")
    monkeypatch.setattr(config, "PICKUP_PASSWORD", "")
    monkeypatch.setattr(database, "_CONN_POOL", [])
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE orders (
                internal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code TEXT UNIQUE, round_number INTEGER NOT NULL,
                status TEXT NOT NULL, payment_status TEXT NOT NULL,
                khantok_ticket INTEGER NOT NULL DEFAULT 0,
                khantok_ticket_claimed_at TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, product_slug TEXT NOT NULL,
                product_name TEXT NOT NULL, product_short_name TEXT NOT NULL,
                product_tagline TEXT NOT NULL, product_price INTEGER NOT NULL,
                product_image TEXT NOT NULL, product_category TEXT NOT NULL,
                size TEXT NOT NULL, quantity INTEGER NOT NULL,
                total_amount INTEGER NOT NULL, items_json TEXT NOT NULL DEFAULT '[]',
                student_code TEXT NOT NULL DEFAULT '', full_name TEXT NOT NULL DEFAULT '',
                parent_phone TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL,
                last_name TEXT NOT NULL, nickname TEXT NOT NULL, email TEXT NOT NULL,
                phone TEXT NOT NULL, school TEXT NOT NULL, access_token TEXT NOT NULL,
                slip_original_name TEXT, slip_stored_name TEXT, slip_storage_path TEXT,
                slip_mime_type TEXT, slip_size INTEGER, slip_uploaded_at TEXT
            );
            INSERT INTO orders (
                order_code, round_number, status, payment_status, created_at, updated_at,
                product_slug, product_name, product_short_name, product_tagline,
                product_price, product_image, product_category, size, quantity,
                total_amount, first_name, last_name, nickname, email, phone, school,
                access_token
            ) VALUES (
                'FP28-TEST', 1, 'paid', 'paid', '2026-01-01', '2026-01-01',
                'single', 'Legacy', 'Legacy', '', 1, '', 'single', 'M', 1, 1,
                'A', 'B', 'C', 'a@example.com', '0800000000', 'School', 'token'
            );
            """
        )
        before = conn.execute("SELECT * FROM orders").fetchall()

    database.ensure_db()

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM orders").fetchall() == before
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"admin_users", "site_settings", "order_audit_log", "slip_ocr_results"} <= tables
        assert "store_products" in tables
        assert "products" not in tables


def test_no_fp28_package():
    assert not (SERVER / "legacy_fp28").exists()
