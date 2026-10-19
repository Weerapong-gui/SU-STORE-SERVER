import json
import sqlite3
import sys
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fp28 import config, display


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE orders (
            internal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT,
            items_json TEXT NOT NULL DEFAULT '[]',
            product_slug TEXT NOT NULL DEFAULT '',
            product_name TEXT NOT NULL DEFAULT '',
            size TEXT NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 0,
            student_code TEXT NOT NULL DEFAULT '',
            nickname TEXT NOT NULL DEFAULT '',
            full_name TEXT NOT NULL DEFAULT '',
            khantok_ticket INTEGER NOT NULL DEFAULT 0,
            khantok_ticket_value INTEGER,
            round_number INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE order_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT,
            event TEXT,
            detail TEXT,
            created_at TEXT
        );
        CREATE TABLE display2_picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_internal_id INTEGER NOT NULL,
            order_code TEXT NOT NULL,
            item_index INTEGER NOT NULL,
            station TEXT NOT NULL,
            product_slug TEXT NOT NULL,
            product_name TEXT NOT NULL,
            size TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            student_code TEXT NOT NULL,
            nickname TEXT NOT NULL,
            full_name TEXT NOT NULL,
            queued_at TEXT NOT NULL,
            queued_by TEXT NOT NULL,
            picked_at TEXT,
            picked_by TEXT,
            undone_at TEXT
        );
        """
    )
    return conn


def _insert_order(conn, code, items, **extra):
    defaults = dict(
        order_code=code,
        items_json=json.dumps(items),
        product_slug=items[0]["slug"] if items else "",
        product_name=items[0].get("name", "") if items else "",
        size=items[0].get("size", "") if items else "",
        quantity=items[0].get("quantity", 1) if items else 0,
        student_code="6931501178",
        nickname="นัท",
        full_name="Park Test",
    )
    defaults.update(extra)
    cols = ",".join(defaults.keys())
    qs = ",".join("?" for _ in defaults)
    conn.execute(f"INSERT INTO orders ({cols}) VALUES ({qs})", tuple(defaults.values()))
    return conn.execute("SELECT * FROM orders WHERE order_code=?", (code,)).fetchone()


class StationMappingTest(unittest.TestCase):
    def test_known_slugs_map_to_stations(self):
        self.assertEqual(config.STATION_BY_SLUG["single"], "polo")
        self.assertEqual(config.STATION_BY_SLUG["jacket"], "jacket")
        self.assertEqual(config.STATION_BY_SLUG["headband"], "headband")

    def test_unknown_slug_returns_none(self):
        self.assertIsNone(config.STATION_BY_SLUG.get("unknown_product"))


class EnqueueDisplay2Test(unittest.TestCase):
    def test_multi_item_order_splits_into_three_rows(self):
        conn = _make_conn()
        items = [
            {"slug": "single", "name": "Polo", "size": "M", "quantity": 1},
            {"slug": "jacket", "name": "Jacket", "size": "L", "quantity": 1},
            {"slug": "headband", "name": "Headband", "size": "FREE", "quantity": 1},
        ]
        row = _insert_order(conn, "FP280001", items)

        inserted = display.enqueue_display2(conn, row, "park")

        self.assertEqual(inserted, 3)
        picks = conn.execute(
            "SELECT station, item_index, queued_by FROM display2_picks ORDER BY item_index"
        ).fetchall()
        self.assertEqual([p["station"] for p in picks], ["polo", "jacket", "headband"])
        self.assertEqual([p["item_index"] for p in picks], [0, 1, 2])
        self.assertTrue(all(p["queued_by"] == "park" for p in picks))

    def test_unknown_slug_is_skipped_and_audit_logged(self):
        conn = _make_conn()
        items = [
            {"slug": "single", "size": "M", "quantity": 1},
            {"slug": "mystery_box", "size": "X", "quantity": 1},
        ]
        row = _insert_order(conn, "FP280002", items)

        inserted = display.enqueue_display2(conn, row, "park")

        self.assertEqual(inserted, 1)
        events = {
            r["event"] for r in conn.execute(
                "SELECT event FROM order_audit_log WHERE order_code=?",
                ("FP280002",),
            )
        }
        self.assertIn("display2_enqueued", events)
        self.assertIn("display2_skipped", events)

    def test_empty_items_json_falls_back_to_order_columns(self):
        conn = _make_conn()
        row = _insert_order(
            conn,
            "FP280003",
            items=[],
            product_slug="single",
            product_name="Polo",
            size="L",
            quantity=2,
        )

        inserted = display.enqueue_display2(conn, row, "park2")

        self.assertEqual(inserted, 1)
        pick = conn.execute("SELECT * FROM display2_picks").fetchone()
        self.assertEqual(pick["station"], "polo")
        self.assertEqual(pick["size"], "L")
        self.assertEqual(pick["quantity"], 2)

    def test_idempotent_on_repeated_call(self):
        conn = _make_conn()
        items = [{"slug": "single", "size": "M", "quantity": 1}]
        row = _insert_order(conn, "FP280004", items)

        first = display.enqueue_display2(conn, row, "park")
        second = display.enqueue_display2(conn, row, "park")

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        count = conn.execute("SELECT COUNT(*) AS c FROM display2_picks").fetchone()["c"]
        self.assertEqual(count, 1)


class ReceivedHookEnqueuesTest(unittest.TestCase):
    """Direct call confirming the helper produces the row the handler expects."""

    def test_received_triggers_enqueue(self):
        conn = _make_conn()
        items = [{"slug": "single", "size": "M", "quantity": 1}]
        row = _insert_order(conn, "FP280010", items)

        count = display.enqueue_display2(conn, row, "park2")

        self.assertEqual(count, 1)
        queued = conn.execute(
            "SELECT queued_by FROM display2_picks WHERE order_code=?",
            ("FP280010",),
        ).fetchone()
        self.assertEqual(queued["queued_by"], "park2")


class KhantokEnqueueTest(unittest.TestCase):
    ITEMS: ClassVar[list[dict]] = [{"slug": "single", "size": "M", "quantity": 1}]

    def test_phase4_receive_skips_khantok_row(self):
        conn = _make_conn()
        row = _insert_order(conn, "FP280014", self.ITEMS,
                            khantok_ticket=1, khantok_ticket_value=100, round_number=4)
        display.enqueue_display2(conn, row, "staff1")
        stations = [r["station"] for r in conn.execute(
            "SELECT station FROM display2_picks WHERE order_code=?", ("FP280014",)
        ).fetchall()]
        self.assertEqual(stations, ["polo"])  # product row only, no khantok

    def test_other_phase_receive_also_skips_khantok_row(self):
        # Finding 1 (final review): the round_number != 4 carve-out in
        # enqueue_display2() is gone entirely -- every phase now hands out the
        # khantok ticket only via mark_khantok_station_claimed()'s own button, so
        # no round should get a khantok row from enqueue_display2() anymore.
        conn = _make_conn()
        row = _insert_order(conn, "FP280013", self.ITEMS,
                            khantok_ticket=1, khantok_ticket_value=50, round_number=3)
        display.enqueue_display2(conn, row, "staff1")
        stations = sorted(r["station"] for r in conn.execute(
            "SELECT station FROM display2_picks WHERE order_code=?", ("FP280013",)
        ).fetchall())
        self.assertEqual(stations, ["polo"])

    def test_helper_inserts_khantok_row_idempotently(self):
        conn = _make_conn()
        row = _insert_order(conn, "FP280015", self.ITEMS,
                            khantok_ticket=1, khantok_ticket_value=100, round_number=4)
        first = display.enqueue_display2_khantok(conn, row, "staff1")
        second = display.enqueue_display2_khantok(conn, row, "staff1")
        self.assertEqual((first, second), (1, 0))
        picked = conn.execute(
            "SELECT station, item_index, size FROM display2_picks WHERE order_code=?",
            ("FP280015",),
        ).fetchall()
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["station"], "khantok")
        self.assertEqual(picked[0]["item_index"], -1)
        self.assertEqual(picked[0]["size"], "฿100")

    def test_helper_returns_zero_without_ticket(self):
        conn = _make_conn()
        row = _insert_order(conn, "FP280016", self.ITEMS,
                            khantok_ticket=0, round_number=4)
        self.assertEqual(display.enqueue_display2_khantok(conn, row, "staff1"), 0)


if __name__ == "__main__":
    unittest.main()
