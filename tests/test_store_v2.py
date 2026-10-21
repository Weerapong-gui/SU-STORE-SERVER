"""SU STORE v2 (`server/store/`) — catalog, order codes, server-side pricing, stock,
and an HTTP round-trip through OrderRequestHandler."""
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("ocr", MagicMock())

import order_api  # noqa: E402
from core import auth, config, database, slips  # noqa: E402
from store import categories, dashboard, db, home  # noqa: E402
from store import meta as store_meta  # noqa: E402


def _conn(path=":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # site_settings is a v1 table the store reads/writes only for site_closed
    conn.execute("CREATE TABLE IF NOT EXISTS site_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.ensure_store_db(conn)
    return conn


def _product(conn, *, stock=5, variants=None, buyer_fields=None, status="active"):
    return db.create_product(
        conn,
        {
            "name": "SU Tote Bag",
            "variants": variants or [{"size": "", "color": "Black", "price": 150, "stock": stock}],
            "buyerFields": buyer_fields or [],
            "status": status,
        },
    )


CUSTOMER = {"name": "Somchai", "phone": "0812345678", "email": "s@example.com"}


class TestOrderCode:
    def test_sequence_and_monthly_reset(self):
        conn = _conn()
        oct_ = datetime(2026, 10, 9, tzinfo=db.TZ_BANGKOK)
        nov = datetime(2026, 11, 1, tzinfo=db.TZ_BANGKOK)
        assert db.next_order_code(conn, oct_) == "SU2610-0001"
        assert db.next_order_code(conn, oct_) == "SU2610-0002"
        assert db.next_order_code(conn, nov) == "SU2611-0001"
        assert db.next_order_code(conn, oct_) == "SU2610-0003"

    def test_period_uses_bangkok_time(self):
        conn = _conn()
        # 2026-10-31 18:00 UTC is already 1 Nov in Bangkok
        utc = datetime(2026, 10, 31, 18, 0, tzinfo=db.timezone.utc)
        assert db.next_order_code(conn, utc) == "SU2611-0001"


class TestProducts:
    def test_create_generates_unique_slug(self):
        conn = _conn()
        a, b = _product(conn), _product(conn)
        assert a["slug"] == "su-tote-bag"
        assert b["slug"] == "su-tote-bag-2"

    def test_cannot_activate_without_variants(self):
        conn = _conn()
        p = db.create_product(conn, {"name": "Draft"})
        with pytest.raises(db.StoreError):
            db.update_product(conn, p["id"], {"status": "active"})

    def test_public_list_hides_drafts(self):
        conn = _conn()
        _product(conn)
        _product(conn, status="draft")
        assert len(db.list_products(conn, public=True)) == 1
        assert len(db.list_products(conn, public=False)) == 2

    def test_duplicate_variant_rejected(self):
        conn = _conn()
        with pytest.raises(db.StoreError):
            _product(conn, variants=[{"size": "M", "price": 1}, {"size": "M", "price": 2}])

    def test_removed_variant_that_was_ordered_is_deactivated(self):
        conn = _conn()
        p = _product(conn)
        vid = p["variants"][0]["id"]
        db.create_order(conn, {"items": [{"variantId": vid, "quantity": 1}], "customer": CUSTOMER})
        db.replace_variants(conn, p["id"], [{"size": "L", "price": 200}])
        rows = conn.execute("SELECT id, active FROM store_variants WHERE product_id = ?", (p["id"],)).fetchall()
        assert {(r["id"], r["active"]) for r in rows if r["id"] == vid} == {(vid, 0)}

    def test_delete_ordered_product_archives(self):
        conn = _conn()
        p = _product(conn)
        db.create_order(conn, {"items": [{"variantId": p["variants"][0]["id"], "quantity": 1}], "customer": CUSTOMER})
        assert db.delete_product(conn, p["id"]) == {"deleted": False, "archived": True}


class TestOrders:
    def test_price_comes_from_server(self):
        conn = _conn()
        vid = _product(conn)["variants"][0]["id"]
        order = db.create_order(
            conn,
            {"items": [{"variantId": vid, "quantity": 2, "unitPrice": 1}], "totalAmount": 1, "customer": CUSTOMER},
        )
        assert order["totalAmount"] == 300
        assert order["orderCode"].startswith("SU")
        assert order["accessToken"]

    def test_stock_decrements_and_blocks_oversell(self):
        conn = _conn()
        vid = _product(conn, stock=2)["variants"][0]["id"]
        db.create_order(conn, {"items": [{"variantId": vid, "quantity": 2}], "customer": CUSTOMER})
        with pytest.raises(db.StoreError) as err:
            db.create_order(conn, {"items": [{"variantId": vid, "quantity": 1}], "customer": CUSTOMER})
        assert err.value.status == 409
        assert conn.execute("SELECT stock FROM store_variants WHERE id = ?", (vid,)).fetchone()[0] == 0

    def test_failed_order_rolls_back_stock_and_counter(self):
        conn = _conn()
        vid = _product(conn, stock=3)["variants"][0]["id"]
        with pytest.raises(db.StoreError):
            db.create_order(conn, {"items": [{"variantId": vid, "quantity": 1}], "customer": {"name": "x"}})
        assert conn.execute("SELECT stock FROM store_variants WHERE id = ?", (vid,)).fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM store_counters").fetchone()[0] == 0

    def test_concurrent_buyers_do_not_oversell(self, tmp_path):
        path = str(tmp_path / "store.db")
        setup = _conn(path)
        vid = _product(setup, stock=3)["variants"][0]["id"]
        setup.commit()
        results = []

        def buy():
            conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                db.create_order(conn, {"items": [{"variantId": vid, "quantity": 1}], "customer": CUSTOMER})
                results.append("ok")
            except db.StoreError:
                results.append("sold_out")

        threads = [threading.Thread(target=buy) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("ok") == 3
        assert setup.execute("SELECT stock FROM store_variants WHERE id = ?", (vid,)).fetchone()[0] == 0
        codes = [r[0] for r in setup.execute("SELECT order_code FROM store_orders")]
        assert len(set(codes)) == 3

    def test_unlimited_stock(self):
        conn = _conn()
        vid = _product(conn, variants=[{"price": 10, "stock": None}])["variants"][0]["id"]
        db.create_order(conn, {"items": [{"variantId": vid, "quantity": 50}], "customer": CUSTOMER})
        assert conn.execute("SELECT stock FROM store_variants WHERE id = ?", (vid,)).fetchone()[0] is None

    def test_product_buyer_fields_are_required(self):
        conn = _conn()
        vid = _product(conn, buyer_fields=["studentCode"])["variants"][0]["id"]
        with pytest.raises(db.StoreError):
            db.create_order(conn, {"items": [{"variantId": vid, "quantity": 1}], "customer": CUSTOMER})
        order = db.create_order(
            conn, {"items": [{"variantId": vid, "quantity": 1}], "customer": {**CUSTOMER, "studentCode": "6831501178"}}
        )
        assert order["customer"]["studentCode"] == "6831501178"

    def test_draft_product_cannot_be_ordered(self):
        conn = _conn()
        vid = _product(conn, status="draft")["variants"][0]["id"]
        with pytest.raises(db.StoreError) as err:
            db.create_order(conn, {"items": [{"variantId": vid, "quantity": 1}], "customer": CUSTOMER})
        assert err.value.status == 409

    def test_cancel_returns_stock_and_is_final(self):
        conn = _conn()
        vid = _product(conn, stock=2)["variants"][0]["id"]
        order = db.create_order(conn, {"items": [{"variantId": vid, "quantity": 2}], "customer": CUSTOMER})
        db.update_order(conn, order["orderCode"], {"status": "cancelled"})
        assert conn.execute("SELECT stock FROM store_variants WHERE id = ?", (vid,)).fetchone()[0] == 2
        with pytest.raises(db.StoreError):
            db.update_order(conn, order["orderCode"], {"status": "paid"})

    def test_lookup_by_code_and_phone(self):
        conn = _conn()
        vid = _product(conn)["variants"][0]["id"]
        order = db.create_order(conn, {"items": [{"variantId": vid, "quantity": 1}], "customer": CUSTOMER})
        found = db.lookup_order(conn, order["orderCode"].lower(), "081-234-5678")
        assert found["accessToken"] == order["accessToken"]
        for code, phone in [(order["orderCode"], "0899999999"), ("SU0000-0001", CUSTOMER["phone"]), (order["orderCode"], "")]:
            with pytest.raises(db.StoreError) as err:
                db.lookup_order(conn, code, phone)
            assert err.value.status == 404

    def test_csv_export_has_one_row_per_item(self):
        conn = _conn()
        p = _product(conn, variants=[{"size": "M", "price": 100}, {"size": "L", "price": 120}])
        db.create_order(
            conn,
            {"items": [{"variantId": v["id"], "quantity": 1} for v in p["variants"]], "customer": CUSTOMER},
        )
        rows = db.orders_csv_rows(conn)
        assert len(rows) == 3 and rows[0][0] == "เลขออเดอร์"

    def test_same_checkout_id_returns_the_existing_order(self):
        conn = _conn()
        vid = _product(conn, stock=5)["variants"][0]["id"]
        body = {"items": [{"variantId": vid, "quantity": 2}], "customer": CUSTOMER,
                "checkoutId": "3f2a9c1e-7b4d-4e8a-9c0f-1a2b3c4d5e6f"}
        first = db.create_order(conn, body)
        again = db.create_order(conn, body)  # retry after a lost response / second tab
        assert again["orderCode"] == first["orderCode"] and again["accessToken"] == first["accessToken"]
        assert conn.execute("SELECT COUNT(*) FROM store_orders").fetchone()[0] == 1
        assert conn.execute("SELECT stock FROM store_variants WHERE id = ?", (vid,)).fetchone()[0] == 3
        other = db.create_order(conn, {**body, "checkoutId": "another-checkout-id-0001"})
        assert other["orderCode"] != first["orderCode"]
        with pytest.raises(db.StoreError):
            db.create_order(conn, {**body, "checkoutId": "short"})

    def test_expected_total_must_match_current_prices(self):
        conn = _conn()
        p = _product(conn, variants=[{"size": "M", "price": 100, "stock": 5}])
        vid = p["variants"][0]["id"]
        db.replace_variants(conn, p["id"], [{"id": vid, "size": "M", "price": 120, "stock": 5}])
        with pytest.raises(db.StoreError) as err:
            db.create_order(conn, {"items": [{"variantId": vid, "quantity": 2}], "customer": CUSTOMER,
                                   "expectedTotal": 200})
        assert err.value.status == 409
        assert conn.execute("SELECT stock FROM store_variants WHERE id = ?", (vid,)).fetchone()[0] == 5
        order = db.create_order(conn, {"items": [{"variantId": vid, "quantity": 2}], "customer": CUSTOMER,
                                       "expectedTotal": 240})
        assert order["totalAmount"] == 240

    def test_mixed_cart_rejects_whole_order_and_keeps_stock(self):
        conn = _conn()
        ok = _product(conn, stock=5)["variants"][0]["id"]
        ended = db.create_product(conn, {"name": "Old Shirt", "status": "active",
                                         "saleEndsAt": "2020-01-01T00:00:00+07:00",
                                         "variants": [{"price": 50, "stock": 5}]})["variants"][0]["id"]
        with pytest.raises(db.StoreError) as err:
            db.create_order(conn, {"items": [{"variantId": ok, "quantity": 1}, {"variantId": ended, "quantity": 1}],
                                   "customer": CUSTOMER})
        assert err.value.status == 409
        assert conn.execute("SELECT stock FROM store_variants WHERE id = ?", (ok,)).fetchone()[0] == 5

    def test_csv_export_neutralises_formulas(self):
        conn = _conn()
        vid = _product(conn)["variants"][0]["id"]
        db.create_order(conn, {"items": [{"variantId": vid, "quantity": 1}],
                               "customer": {**CUSTOMER, "name": '=HYPERLINK("http://x","y")'}})
        row = db.orders_csv_rows(conn)[1]
        assert '\'=HYPERLINK("http://x","y")' in row
        assert 150 in row  # numbers stay numbers

    def test_stale_cancel_does_not_return_stock_twice(self, monkeypatch):
        conn = _conn()
        vid = _product(conn, stock=2)["variants"][0]["id"]
        order = db.create_order(conn, {"items": [{"variantId": vid, "quantity": 2}], "customer": CUSTOMER})
        stale = db.get_order_row(conn, order["orderCode"])
        db.update_order(conn, order["orderCode"], {"status": "cancelled"})
        # a second staff member who loaded the order before it was cancelled
        monkeypatch.setattr(db, "_fetch_order_row", lambda *_: stale)
        with pytest.raises(db.StoreError) as err:
            db.update_order(conn, order["orderCode"], {"status": "cancelled"})
        assert err.value.status == 409
        assert conn.execute("SELECT stock FROM store_variants WHERE id = ?", (vid,)).fetchone()[0] == 2


class TestSaleWindow:
    def test_states(self):
        now = datetime(2026, 10, 10, 12, 0, tzinfo=db.TZ_BANGKOK)
        assert db.sale_state(None, None, now) == "open"
        assert db.sale_state("2026-10-11T00:00:00+07:00", None, now) == "upcoming"
        assert db.sale_state(None, "2026-10-10T12:00:00+07:00", now) == "ended"
        assert db.sale_state("2026-10-01T00:00:00+07:00", "2026-10-31T00:00:00+07:00", now) == "open"

    def test_naive_input_is_bangkok_time(self):
        conn = _conn()
        p = _product(conn)
        p = db.update_product(conn, p["id"], {"saleStartsAt": "2026-10-12T10:00", "saleEndsAt": None})
        assert p["saleStartsAt"] == "2026-10-12T10:00:00+07:00"

    def test_end_must_be_after_start(self):
        conn = _conn()
        p = _product(conn)
        with pytest.raises(db.StoreError):
            db.update_product(conn, p["id"], {"saleStartsAt": "2026-10-12T10:00", "saleEndsAt": "2026-10-12T09:00"})

    @pytest.mark.parametrize("window", [{"saleStartsAt": "2999-01-01T00:00"}, {"saleEndsAt": "2000-01-01T00:00"}])
    def test_orders_blocked_outside_window(self, window):
        conn = _conn()
        p = _product(conn)
        db.update_product(conn, p["id"], window)
        with pytest.raises(db.StoreError) as err:
            db.create_order(conn, {"items": [{"variantId": p["variants"][0]["id"], "quantity": 1}], "customer": CUSTOMER})
        assert err.value.status == 409
        assert conn.execute("SELECT stock FROM store_variants").fetchone()[0] == 5

    def test_migration_is_idempotent_and_adds_columns(self):
        conn = _conn()
        db.ensure_store_db(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(store_products)")}
        assert {"sale_starts_at", "sale_ends_at"} <= cols


class TestSettings:
    def test_defaults_and_update(self):
        conn = _conn()
        assert db.get_settings(conn)["payment"]["accountNumber"] == "672-3000-615"
        s = db.update_settings(conn, {"payment": {"bankName": "กสิกรไทย", "accountNumber": "123-4-56789-0",
                                                  "accountName": "องค์การนักศึกษา"},
                                      "announcement": "รับของ 20 ต.ค.", "siteClosed": True})
        assert s["payment"]["bankName"] == "กสิกรไทย" and s["announcement"] == "รับของ 20 ต.ค." and s["siteClosed"]
        assert conn.execute("SELECT value FROM site_settings WHERE key='site_closed'").fetchone()[0] == "1"
        assert db.update_settings(conn, {"siteClosed": False})["siteClosed"] is False

    def test_bad_account_number(self):
        with pytest.raises(db.StoreError):
            db.update_settings(_conn(), {"payment": {"accountNumber": "abc"}})

    def test_meta_includes_payment(self):
        conn = _conn()
        assert store_meta.meta(conn)["payment"]["bankName"] == "ธนาคารกรุงเทพ"


class TestHome:
    HERO: ClassVar[dict[str, Any]] = {"id": "b1", "type": "hero", "slides": [{"title": "Hello", "buttonText": "Shop", "buttonHref": "/products"}]}

    def test_default_until_published(self):
        conn = _conn()
        state = home.get_home_admin(conn)
        assert state["published"] == home.DEFAULT_HOME and not state["dirty"] and state["publishedAt"] is None
        assert store_meta.meta(conn)["accent"] == home.DEFAULT_ACCENT

    def test_draft_publish_discard(self):
        conn = _conn()
        state = home.save_home_draft(conn, {"accent": "#1F7A3A", "blocks": [self.HERO]})
        assert state["dirty"] and state["published"] == home.DEFAULT_HOME
        assert home.resolve_home(conn, state["published"])["accent"] == home.DEFAULT_ACCENT
        state = home.publish_home(conn)
        assert not state["dirty"] and state["published"]["accent"] == "#1f7a3a"
        assert store_meta.meta(conn)["accent"] == "#1f7a3a"
        home.save_home_draft(conn, {"accent": "#1f7a3a", "blocks": []})
        state = home.discard_home_draft(conn)
        assert not state["dirty"] and state["draft"]["blocks"][0]["slides"][0]["title"] == "Hello"

    def test_radius_defaults_and_publishes(self):
        conn = _conn()
        assert home.validate_home({"blocks": []})["radius"] == home.DEFAULT_RADIUS
        assert store_meta.meta(conn)["radius"] == home.DEFAULT_RADIUS
        home.save_home_draft(conn, {"radius": 0, "blocks": [self.HERO]})
        home.publish_home(conn)
        assert store_meta.meta(conn)["radius"] == 0
        assert home.resolve_home(conn, home.get_home_admin(conn)["published"])["radius"] == 0

    def test_block_radius_is_optional(self):
        layout = home.validate_home({"blocks": [{**self.HERO, "radius": 0}, {**self.HERO, "id": "b2", "radius": None}]})
        assert layout["blocks"][0]["radius"] == 0
        assert "radius" not in layout["blocks"][1]

    def test_font_defaults_and_publishes(self):
        conn = _conn()
        assert home.validate_home({"blocks": []})["font"] == home.DEFAULT_FONT
        home.save_home_draft(conn, {"font": "kanit", "blocks": [self.HERO]})
        assert store_meta.meta(conn)["font"] == home.DEFAULT_FONT
        home.publish_home(conn)
        assert store_meta.meta(conn)["font"] == "kanit"

    def test_layout_saved_before_theme_settings_keeps_original_look(self):
        conn = _conn()
        legacy = json.dumps({"accent": "#1f7a3a", "blocks": []})
        conn.execute("INSERT INTO store_settings (key, value, updated_at) VALUES ('home_published', ?, 'x')", (legacy,))
        assert home.get_home_admin(conn)["draft"]["radius"] == home.DEFAULT_RADIUS
        assert store_meta.meta(conn)["radius"] == home.DEFAULT_RADIUS
        assert store_meta.meta(conn)["font"] == home.DEFAULT_FONT

    @pytest.mark.parametrize("payload", [
        {"blocks": [{"id": "x", "type": "marquee"}]},
        {"blocks": [{**HERO, "slides": [{"buttonText": "Go", "buttonHref": "javascript:alert(1)"}]}]},
        {"blocks": [{**HERO, "slides": [{"buttonText": "Go", "buttonHref": "//evil.example"}]}]},
        {"blocks": [{**HERO, "slides": [{"buttonText": "Go"}]}]},
        {"blocks": [{**HERO, "slides": [{"image": "https://evil.example/a.png"}]}]},
        {"blocks": [{**HERO, "slides": []}]},
        {"blocks": [HERO, HERO]},
        {"accent": "#ffeb3b", "blocks": []},
        {"accent": "red", "blocks": []},
        {"radius": -1, "blocks": []},
        {"radius": 101, "blocks": []},
        {"radius": "abc", "blocks": []},
        {"radius": 50.5, "blocks": []},
        {"radius": True, "blocks": []},
        {"font": "comic-sans", "blocks": []},
        {"blocks": [{**HERO, "radius": 150}]},
        {"blocks": [{**HERO, "radius": "square"}]},
        {"font": None, "blocks": []},
        {"blocks": [{"id": "f", "type": "featured", "productIds": list(range(13))}]},
        {"blocks": [{"id": "c", "type": "categories", "categoryIds": list(range(13))}]},
        {"blocks": [{"id": "c", "type": "categories", "title": "x" * 81}]},
        {"blocks": [{"id": "c", "type": "categories", "categoryIds": "1"}]},
    ])
    def test_rejects_bad_layouts(self, payload):
        with pytest.raises(db.StoreError):
            home.validate_home(payload)

    def test_categories_block_resolves_visible_only(self):
        conn = _conn()
        first = categories.create_category(conn, {"name": "First"})
        hidden = categories.create_category(conn, {"name": "Hidden", "visible": False})
        last = categories.create_category(conn, {"name": "Last"})
        categories.reorder_categories(conn, [last["id"], hidden["id"], first["id"]])
        layout = home.validate_home({"blocks": [
            {"id": "all", "type": "categories", "title": "x" * 80, "categoryIds": []},
            {"id": "selected", "type": "categories", "categoryIds": [first["id"], hidden["id"], last["id"], 999]},
            {"id": "one", "type": "categories", "categoryIds": [first["id"]]},
            {"id": "hidden", "type": "categories", "hidden": True},
            {"id": "limit", "type": "categories", "categoryIds": list(range(12))},
        ]})
        resolved = home.resolve_home(conn, layout)
        assert [b["id"] for b in resolved["blocks"]] == ["all", "selected", "one", "limit"]
        for block in resolved["blocks"][:2]:
            assert [c["id"] for c in block["categories"]] == [last["id"], first["id"]]
            assert all(c["visible"] for c in block["categories"])
        assert resolved["blocks"][0]["title"] == "x" * 80
        assert [c["name"] for c in resolved["blocks"][2]["categories"]] == ["First"]
        assert "categories" not in layout["blocks"][0]

    def test_categories_block_dropped_when_none_visible(self):
        conn = _conn()
        hidden = categories.create_category(conn, {"name": "Hidden", "visible": False})
        layout = home.validate_home({"blocks": [
            {"id": "all", "type": "categories", "categoryIds": []},
            {"id": "selected", "type": "categories", "categoryIds": [hidden["id"], 999]},
        ]})
        assert home.resolve_home(conn, layout)["blocks"] == []
        categories.create_category(conn, {"name": "Visible"})
        assert [b["id"] for b in home.resolve_home(conn, layout)["blocks"]] == ["all"]

    @pytest.mark.parametrize("value", ["1", True, False, 1.5, None, {}, []])
    def test_categories_block_rejects_non_int_ids(self, value):
        with pytest.raises(db.StoreError, match="รายการหมวดหมู่ไม่ถูกต้อง") as error:
            home.validate_home({"blocks": [{"id": "c", "type": "categories", "categoryIds": [value]}]})
        assert error.value.status == 400

    def test_resolve_skips_hidden_empty_and_unavailable(self):
        conn = _conn()
        live = _product(conn)
        archived = _product(conn)
        db.update_product(conn, archived["id"], {"status": "archived"})
        layout = home.validate_home({"blocks": [
            {"id": "a", "type": "featured", "productIds": [archived["id"], live["id"], 999]},
            {"id": "b", "type": "featured", "productIds": [archived["id"]]},
            {"id": "c", "type": "text", "title": "", "body": ""},
            {"id": "d", "type": "text", "title": "Pickup", "hidden": True},
            {"id": "e", "type": "allProducts"},
        ]})
        resolved = home.resolve_home(conn, layout)
        assert [b["id"] for b in resolved["blocks"]] == ["a", "e"]
        assert [p["id"] for p in resolved["blocks"][0]["products"]] == [live["id"]]
        assert [p["id"] for p in resolved["products"]] == [live["id"]]


class TestDashboard:
    def test_totals_and_breakdown(self):
        conn = _conn()
        p = _product(conn, variants=[{"size": "M", "price": 100, "stock": None}, {"size": "L", "price": 150, "stock": None}])
        m, large = (v["id"] for v in p["variants"])

        def buy(items):
            return db.create_order(conn, {"items": items, "customer": CUSTOMER})["orderCode"]

        paid = buy([{"variantId": m, "quantity": 2}, {"variantId": large, "quantity": 1}])  # 350
        db.update_order(conn, paid, {"status": "completed"})
        buy([{"variantId": m, "quantity": 1}])  # 100 pending
        cancelled = buy([{"variantId": large, "quantity": 3}])
        db.update_order(conn, cancelled, {"status": "cancelled"})

        d = dashboard.dashboard(conn, days=7)
        assert d["paidAmount"] == 350 and d["awaitingAmount"] == 100 and d["orderCount"] == 2
        assert d["statusCounts"]["cancelled"] == 1
        assert len(d["byDay"]) == 7 and d["byDay"][-1]["paidAmount"] == 350 and d["byDay"][-1]["orders"] == 2
        rows = {r["variantLabel"]: r for r in d["byVariant"]}
        assert rows["M"]["paidQuantity"] == 2 and rows["M"]["unpaidQuantity"] == 1 and rows["M"]["paidAmount"] == 200
        assert rows["L"]["paidQuantity"] == 1  # cancelled order excluded


def test_ensure_db_leaves_v1_orders_untouched(tmp_path, monkeypatch):
    """Running the v2 migration on a DB with FP28 data must not change v1 rows."""
    path = tmp_path / "orders.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "SLIPS_DIR", tmp_path / "slips")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE orders (order_code TEXT, status TEXT, payment_status TEXT, created_at TEXT)"
        )
    database.ensure_db()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO orders (order_code, status, payment_status, created_at) "
            "VALUES ('FP2800011', 'paid', 'paid', 't')"
        )
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM orders").fetchall()
    database.ensure_db()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM orders").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM store_products").fetchone()[0] == 0


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setattr(config, "SLIPS_DIR", tmp_path / "slips")
    monkeypatch.setattr(config, "ORDER_API_TOKEN", "test-token")
    monkeypatch.setattr(slips, "trigger_ocr_async", lambda *a, **k: None)
    monkeypatch.setattr(database, "_CONN_POOL", [])
    database.ensure_db()
    with sqlite3.connect(tmp_path / "orders.db") as conn:
        conn.execute(
            "INSERT INTO admin_users (username, claim_token, is_superadmin, created_at) VALUES (?, ?, 0, 't')",
            ("staff1", auth.compute_claim_token("staff1", "pw")),
        )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), order_api.OrderRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _call(base, method, path, body=None, headers=None, raw=None, content_type=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", content_type or "application/json")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_http_round_trip(server):
    status, body = _call(server, "POST", "/v2/admin/login", {"username": "staff1", "password": "wrong"})
    assert status == 401
    status, body = _call(server, "POST", "/v2/admin/login", {"username": "staff1", "password": "pw"})
    assert status == 200
    admin = {"Authorization": "Claim " + json.loads(body)["token"]}

    assert _call(server, "GET", "/v2/admin/products")[0] == 401
    status, body = _call(
        server, "POST", "/v2/admin/products",
        {"name": "Hoodie", "status": "active", "buyerFields": ["studentCode"],
         "variants": [{"size": "M", "color": "Navy", "price": 590, "stock": 1}]},
        admin,
    )
    assert status == 201, body
    product = json.loads(body)

    status, body = _call(server, "GET", "/v2/products")
    assert [p["slug"] for p in json.loads(body)["products"]] == ["hoodie"]

    order_body = {"items": [{"variantId": product["variants"][0]["id"], "quantity": 1}],
                  "customer": {**CUSTOMER, "studentCode": "6800000001"}}
    status, body = _call(server, "POST", "/v2/orders", order_body)
    assert status == 201, body
    order = json.loads(body)
    assert order["totalAmount"] == 590
    code, token = order["orderCode"], order["accessToken"]
    assert _call(server, "POST", "/v2/orders", order_body)[0] == 409  # sold out

    assert _call(server, "GET", f"/v2/orders/{code}")[0] == 401
    assert _call(server, "GET", f"/v2/orders/{code}?token={token}")[0] == 200

    boundary = "xxBOUNDARYxx"
    multipart = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"slip\"; filename=\"s.png\"\r\n"
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + b"\x89PNG fake" + f"\r\n--{boundary}--\r\n".encode()
    status, body = _call(server, "POST", f"/v2/orders/{code}/slip", headers={"X-Order-Token": token},
                         raw=multipart, content_type=f"multipart/form-data; boundary={boundary}")
    assert status == 200, body
    assert json.loads(body)["status"] == "waiting_confirm"

    status, body = _call(server, "PATCH", f"/v2/admin/orders/{code}", {"status": "paid"}, admin)
    assert status == 200 and json.loads(body)["statusLabel"] == "ชำระแล้ว"
    status, body = _call(server, "GET", "/v2/admin/orders?status=paid", headers=admin)
    assert json.loads(body)["total"] == 1
    status, body = _call(server, "GET", f"/v2/admin/orders/{code}/slip", headers=admin)
    assert status == 200 and body.startswith(b"\x89PNG")
    status, body = _call(server, "GET", "/v2/admin/orders.csv", headers=admin)
    assert status == 200 and "Hoodie" in body.decode("utf-8-sig")

    # v1 endpoints still served
    assert _call(server, "GET", "/health")[0] == 200


def test_home_http(server):
    _, body = _call(server, "POST", "/v2/admin/login", {"username": "staff1", "password": "pw"})
    admin = {"Authorization": "Claim " + json.loads(body)["token"]}
    assert _call(server, "GET", "/v2/admin/home")[0] == 401
    assert _call(server, "POST", "/v2/admin/home/publish")[0] == 401
    layout = {"accent": "#7b1fa2", "blocks": [{"id": "t", "type": "text", "title": "Hi"}]}
    status, _ = _call(server, "PUT", "/v2/admin/home", layout, admin)
    assert status == 200
    status, body = _call(server, "GET", "/v2/admin/home/preview", headers=admin)
    assert json.loads(body)["blocks"][0]["title"] == "Hi"
    assert json.loads(_call(server, "GET", "/v2/home")[1])["accent"] == home.DEFAULT_ACCENT
    assert _call(server, "POST", "/v2/admin/home/publish", headers=admin)[0] == 200
    published = json.loads(_call(server, "GET", "/v2/home")[1])
    assert published["accent"] == "#7b1fa2" and published["blocks"][0]["title"] == "Hi"


def test_settings_changes_are_audited(server):
    _, body = _call(server, "POST", "/v2/admin/login", {"username": "staff1", "password": "pw"})
    admin = {"Authorization": "Claim " + json.loads(body)["token"]}
    status, _ = _call(server, "PUT", "/v2/admin/settings",
                      {"payment": {"accountNumber": "111-222-333"}, "announcement": "hi"}, admin)
    assert status == 200
    status, _ = _call(server, "PUT", "/v2/admin/settings", {"announcement": "hi"}, admin)  # no change
    with sqlite3.connect(config.DB_PATH) as conn:
        events = conn.execute("SELECT event, detail FROM order_audit_log WHERE event LIKE 'v2_%' ORDER BY id").fetchall()
    assert [e for e, _ in events] == ["v2_payment_changed", "v2_announcement_changed"]
    assert "672-3000-615" in events[0][1] and "111-222-333" in events[0][1] and "by staff1" in events[0][1]


def test_oversized_bodies_are_refused_before_reading(server):
    import http.client

    def send(path, content_type, length):
        host, port = server.removeprefix("http://").split(":")
        c = http.client.HTTPConnection(host, int(port), timeout=5)
        c.putrequest("POST", path)
        c.putheader("Content-Type", content_type)
        c.putheader("Content-Length", str(length))
        c.endheaders()  # no body sent: the server must answer without waiting for it
        status = c.getresponse().status
        c.close()
        return status

    assert send("/v2/orders", "application/json", 10 * 1024 * 1024) == 400
    _, body = _call(server, "POST", "/v2/admin/login", {"username": "staff1", "password": "pw"})
    admin = {"Authorization": "Claim " + json.loads(body)["token"]}
    _, body = _call(server, "POST", "/v2/admin/products",
                    {"name": "Cap", "status": "active", "variants": [{"price": 100, "stock": 5}]}, admin)
    vid = json.loads(body)["variants"][0]["id"]
    _, body = _call(server, "POST", "/v2/orders", {"items": [{"variantId": vid, "quantity": 1}], "customer": CUSTOMER})
    order = json.loads(body)
    path = f"/v2/orders/{order['orderCode']}/slip?token={order['accessToken']}"
    assert send(path, "multipart/form-data; boundary=x", 50 * 1024 * 1024) == 400
