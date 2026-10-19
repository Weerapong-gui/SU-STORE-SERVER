"""Integration tests for the phase-4 khantok station-claim flow.

Uses a real schema via ensure_db() pointed at a pytest tmp_path.
"""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub out OCR module so order_api imports cleanly even without Tesseract
sys.modules.setdefault("ocr", MagicMock())

# Set temp directories to avoid permission errors at module import time
_tmp_for_import = TemporaryDirectory()
os.environ.setdefault("ORDER_API_SLIDES_DIR", str(Path(_tmp_for_import.name) / "slides"))
os.environ.setdefault("ORDER_API_ASSETS_DIR", str(Path(_tmp_for_import.name) / "assets"))

from core import config, database  # noqa: E402
from legacy_fp28 import display, orders, stats  # noqa: E402


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "orders.db")
    monkeypatch.setattr(config, "SLIPS_DIR", tmp_path / "slips")
    monkeypatch.setattr(config, "PRODUCT_IMAGES_DIR", tmp_path / "product-images")
    # _CONN_POOL is module-global and path-unaware: a connection pooled by a previous
    # test would be handed out here still attached to that test's DB file. Give each
    # test its own pool and drain it on teardown.
    pool = []
    monkeypatch.setattr(database, "_CONN_POOL", pool)
    database.ensure_db()
    pooled = database.open_db()
    connection = pooled.__enter__()
    yield connection
    pooled.__exit__(None, None, None)
    for leftover in pool:
        leftover.close()


class TestStationClaimedColumn:
    def test_column_exists_after_ensure_db(self, temp_db):
        columns = {
            row["name"]
            for row in temp_db.execute("PRAGMA table_info(orders)").fetchall()
        }
        assert "khantok_station_claimed_at" in columns

    def test_serializer_exposes_station_claimed_at(self, temp_db):
        temp_db.execute(
            "INSERT INTO orders ("
            "  round_number, status, payment_status, created_at, updated_at,"
            "  order_code, khantok_station_claimed_at,"
            "  product_slug, product_name, product_short_name, product_tagline,"
            "  product_price, product_image, product_category,"
            "  size, quantity, total_amount,"
            "  first_name, last_name, nickname, email, phone, school, access_token"
            ") VALUES ("
            "  4, 'paid', 'paid', '2026-07-19T00:00:00Z', '2026-07-19T00:00:00Z',"
            "  'FP2899994', '2026-07-19T05:00:00Z',"
            "  'single', 'Test Product', 'Test', 'tagline',"
            "  100, 'image.png', 'category',"
            "  'M', 1, 100,"
            "  'John', 'Doe', 'nick', 'test@example.com', '0123456789', 'school', 'token'"
            ")"
        )
        row = orders.fetch_order_by_code(temp_db, "FP2899994")
        data = orders.serialize_order(row)
        assert data["khantokStationClaimedAt"] == "2026-07-19T05:00:00Z"


def _insert_summary_order(connection, code, *, round_number=1, status="received",
                          station_claimed_at=None, received_at=None):
    connection.execute(
        "INSERT INTO orders ("
        "  round_number, status, payment_status, created_at, updated_at, order_code,"
        "  khantok_station_claimed_at, received_at,"
        "  product_slug, product_name, product_short_name, product_tagline,"
        "  product_price, product_image, product_category,"
        "  size, quantity, total_amount,"
        "  first_name, last_name, nickname, email, phone, school, access_token"
        ") VALUES (?, ?, 'paid', '2026-07-19T00:00:00Z', '2026-07-19T00:00:00Z', ?,"
        "  ?, ?,"
        "  'single-shirt', 'FRESHER POLO SHIRT', 'POLO', 'tagline',"
        "  399, '/p.png', 'single',"
        "  'M', 1, 399,"
        "  'John', 'Doe', 'nick', 'a@b.c', '0800000000', 'MFU', ?)",
        (round_number, status, code, station_claimed_at, received_at, "tok-" + code),
    )


class TestSerializerClaimWarning:
    def test_warning_true_for_order_received_on_an_earlier_day(self, temp_db):
        _insert_summary_order(
            temp_db, "FP2899881", status="received",
            station_claimed_at=None, received_at="2020-01-01T10:00:00+07:00",
        )
        row = orders.fetch_order_by_code(temp_db, "FP2899881")
        assert orders.serialize_order(row)["khantokClaimWarning"] is True

    def test_warning_false_when_never_received(self, temp_db):
        _insert_summary_order(
            temp_db, "FP2899882", status="shipped",
            station_claimed_at=None, received_at=None,
        )
        row = orders.fetch_order_by_code(temp_db, "FP2899882")
        assert orders.serialize_order(row)["khantokClaimWarning"] is False


def _order_payload(student_code="6935001234", slug="single"):
    product = {
        "slug": slug, "name": "FRESHER POLO SHIRT", "shortName": "POLO",
        "tagline": "x", "price": 399, "image": "/p.png", "category": "shirt",
    }
    return {
        "product": product,
        "customer": {
            "studentCode": student_code, "email": "a@b.c", "fullName": "Test User",
            "phone": "0800000000", "school": "MFU", "parentPhone": "0811111111",
        },
        "size": "M", "quantity": 1, "totalAmount": 399,
        "items": [{"product": product, "size": "M", "quantity": 1,
                   "unitPrice": 399, "totalAmount": 399}],
    }


def _create_phase4_order(connection, student_code="6935001234", status="paid"):
    connection.execute(
        "INSERT OR REPLACE INTO site_settings (key, value, updated_at) VALUES ('phase_override', '4', '2026-07-19T00:00:00Z')"
    )
    validated, error = orders.validate_order_payload(_order_payload(student_code))
    assert error is None, error
    order = orders.create_order(connection, validated)
    connection.execute(
        "UPDATE orders SET status=?, payment_status='paid' WHERE order_code=?",
        (status, order["id"]),
    )
    connection.commit()
    return order["id"]


def _create_order_for_round(connection, round_number, student_code="6935001234", status="paid"):
    """Like _create_phase4_order but for an arbitrary phase, so Finding-1 tests can
    check that the khantok carve-out is gone for every round, not inverted to round 4."""
    connection.execute(
        "INSERT OR REPLACE INTO site_settings (key, value, updated_at) VALUES"
        " ('phase_override', ?, '2026-07-19T00:00:00Z')",
        (str(round_number),),
    )
    validated, error = orders.validate_order_payload(_order_payload(student_code))
    assert error is None, error
    order = orders.create_order(connection, validated)
    connection.execute(
        "UPDATE orders SET status=?, payment_status='paid' WHERE order_code=?",
        (status, order["id"]),
    )
    connection.commit()
    return order["id"]


def _quota_count(connection):
    return connection.execute(
        "SELECT COUNT(*) AS c FROM khantok_ticket_claims"
    ).fetchone()["c"]


class TestMarkKhantokStationClaimed:
    def test_success_sets_timestamp_enqueues_and_keeps_quota(self, temp_db):
        code = _create_phase4_order(temp_db)
        quota_before = _quota_count(temp_db)
        status, body = orders.mark_khantok_station_claimed(temp_db, code, "staff1")
        temp_db.commit()
        assert status == 200
        assert body["order"]["khantokStationClaimedAt"]
        picks = temp_db.execute(
            "SELECT station, item_index FROM display2_picks WHERE order_code=?", (code,)
        ).fetchall()
        assert [(p["station"], p["item_index"]) for p in picks] == [("khantok", -1)]
        assert _quota_count(temp_db) == quota_before

    def test_double_claim_returns_409(self, temp_db):
        code = _create_phase4_order(temp_db)
        orders.mark_khantok_station_claimed(temp_db, code, "staff1")
        temp_db.commit()
        status, body = orders.mark_khantok_station_claimed(temp_db, code, "staff1")
        assert status == 409
        assert body["claimedAt"]

    def test_order_without_ticket_returns_400(self, temp_db):
        # student code without the 693 prefix -> no ticket reserved
        code = _create_phase4_order(temp_db, student_code="6805001234")
        status, _ = orders.mark_khantok_station_claimed(temp_db, code, "staff1")
        assert status == 400

    def test_unpaid_order_returns_400(self, temp_db):
        code = _create_phase4_order(temp_db, status="pending_payment")
        status, _ = orders.mark_khantok_station_claimed(temp_db, code, "staff1")
        assert status == 400

    def test_unknown_order_returns_404(self, temp_db):
        status, _ = orders.mark_khantok_station_claimed(temp_db, "FP2800000", "staff1")
        assert status == 404


class TestAdminStationClaimedToggle:
    def test_true_sets_timestamp_and_quota_unchanged(self, temp_db):
        code = _create_phase4_order(temp_db)
        quota_before = _quota_count(temp_db)
        result = orders.update_order_fields(
            temp_db, code, {"khantokStationClaimed": True}
        )
        temp_db.commit()
        assert result not in (None, False)
        row = orders.fetch_order_by_code(temp_db, code)
        assert row["khantok_station_claimed_at"]
        assert _quota_count(temp_db) == quota_before

    def test_true_again_does_not_overwrite_timestamp(self, temp_db):
        code = _create_phase4_order(temp_db)
        temp_db.execute(
            "UPDATE orders SET khantok_station_claimed_at='2026-07-01T00:00:00Z'"
            " WHERE order_code=?", (code,),
        )
        orders.update_order_fields(temp_db, code, {"khantokStationClaimed": True})
        row = orders.fetch_order_by_code(temp_db, code)
        assert row["khantok_station_claimed_at"] == "2026-07-01T00:00:00Z"

    def test_false_clears_timestamp(self, temp_db):
        code = _create_phase4_order(temp_db)
        temp_db.execute(
            "UPDATE orders SET khantok_station_claimed_at='2026-07-01T00:00:00Z'"
            " WHERE order_code=?", (code,),
        )
        orders.update_order_fields(temp_db, code, {"khantokStationClaimed": False})
        row = orders.fetch_order_by_code(temp_db, code)
        assert row["khantok_station_claimed_at"] is None


class TestKhantokStationSummaryCounts:
    def test_counts_claimed_and_claimed_but_not_received(self, temp_db):
        _insert_summary_order(temp_db, "FP2890001", status="received",
                              station_claimed_at="2026-07-26T10:00:00+07:00",
                              received_at="2026-07-26T10:00:00+07:00")
        _insert_summary_order(temp_db, "FP2890002", status="shipped",
                              station_claimed_at="2026-07-26T11:00:00+07:00",
                              received_at=None)
        _insert_summary_order(temp_db, "FP2890003", status="received",
                              station_claimed_at=None,
                              received_at="2026-07-26T12:00:00+07:00")
        _insert_summary_order(temp_db, "FP2890004", status="rejected",
                              station_claimed_at="2026-07-26T13:00:00+07:00",
                              received_at=None)
        summary = stats.create_orders_summary(temp_db)
        assert summary["khantokStationClaimed"] == 2
        assert summary["khantokStationClaimedNotReceived"] == 1

    def test_counts_are_zero_on_empty_db(self, temp_db):
        summary = stats.create_orders_summary(temp_db)
        assert summary["khantokStationClaimed"] == 0
        assert summary["khantokStationClaimedNotReceived"] == 0

    def test_round_filter_scopes_the_counts(self, temp_db):
        _insert_summary_order(temp_db, "FP2890011", round_number=1, status="shipped",
                              station_claimed_at="2026-07-26T10:00:00+07:00",
                              received_at=None)
        _insert_summary_order(temp_db, "FP2890012", round_number=4, status="shipped",
                              station_claimed_at="2026-07-26T10:00:00+07:00",
                              received_at=None)
        assert stats.create_orders_summary(temp_db, round_filter=4)["khantokStationClaimed"] == 1
        assert stats.create_orders_summary(temp_db)["khantokStationClaimed"] == 2

    def test_admin_marked_received_without_timestamp_is_not_counted_as_not_received(self, temp_db):
        # Finding 3: an order admin-marked status='received' with no received_at (the
        # claim-station 'received' PATCH is the only path that writes received_at) must
        # still count under khantokStationClaimed, but NOT under the "not received yet"
        # counter -- it's already been received, just not through that endpoint.
        _insert_summary_order(temp_db, "FP2890021", status="received",
                              station_claimed_at="2026-07-26T10:00:00+07:00",
                              received_at=None)
        summary = stats.create_orders_summary(temp_db)
        assert summary["khantokStationClaimed"] == 1
        assert summary["khantokStationClaimedNotReceived"] == 0


class TestEnqueueDisplay2KhantokCarveOutRemoved:
    """Finding 1: enqueue_display2() must never enqueue the khantok pick row for any
    round -- mark_khantok_station_claimed() is the only caller of
    enqueue_display2_khantok() now. Previously round 4 was carved out (skipped); the
    fix removes the carve-out entirely rather than inverting which round it applies to."""

    def test_round_1_ticketed_order_enqueues_merchandise_but_not_khantok(self, temp_db):
        code = _create_order_for_round(temp_db, round_number=1)
        row = orders.fetch_order_by_code(temp_db, code)
        assert row["khantok_ticket"] == 1
        inserted = display.enqueue_display2(temp_db, row, "staff1")
        temp_db.commit()
        assert inserted == 1  # the merchandise item only
        picks = temp_db.execute(
            "SELECT item_index FROM display2_picks WHERE order_code=?", (code,)
        ).fetchall()
        item_indexes = [p["item_index"] for p in picks]
        assert -1 not in item_indexes
        assert 0 in item_indexes

    def test_round_4_ticketed_order_also_does_not_enqueue_khantok(self, temp_db):
        # The carve-out is gone for everyone, not inverted -- round 4 must behave
        # identically to every other round now.
        code = _create_order_for_round(temp_db, round_number=4)
        row = orders.fetch_order_by_code(temp_db, code)
        assert row["khantok_ticket"] == 1
        inserted = display.enqueue_display2(temp_db, row, "staff1")
        temp_db.commit()
        assert inserted == 1  # the merchandise item only
        picks = temp_db.execute(
            "SELECT item_index FROM display2_picks WHERE order_code=?", (code,)
        ).fetchall()
        item_indexes = [p["item_index"] for p in picks]
        assert -1 not in item_indexes
        assert 0 in item_indexes

    def test_mark_khantok_station_claimed_is_the_only_path_that_enqueues_khantok(self, temp_db):
        code = _create_order_for_round(temp_db, round_number=1)
        row = orders.fetch_order_by_code(temp_db, code)
        display.enqueue_display2(temp_db, row, "staff1")  # merchandise only, per above
        temp_db.commit()
        status, body = orders.mark_khantok_station_claimed(temp_db, code, "staff1")
        temp_db.commit()
        assert status == 200
        assert body["queued"] == 1
        khantok_picks = temp_db.execute(
            "SELECT item_index FROM display2_picks WHERE order_code=? AND item_index=-1",
            (code,),
        ).fetchall()
        assert len(khantok_picks) == 1


class TestClaimStationSearchIncludesReceived:
    """Finding 4: an order with status='received' is the only kind that can raise
    khantokClaimWarning, and it must be reachable through claim-station search so a
    returning customer isn't stuck at a 404 with only the (withheld) QR token to fall
    back on. This test suite has no HTTP layer to drive OrderRequestHandler.do_GET
    directly, so it exercises the same query predicate the student-code search branch
    uses (server/order_api.py, the "/claim-station/orders" handler) against temp_db."""

    def test_student_code_search_predicate_returns_a_received_order(self, temp_db):
        _insert_summary_order(temp_db, "FP2890041", status="received",
                              station_claimed_at=None, received_at="2026-07-26T10:00:00+07:00")
        temp_db.execute(
            "UPDATE orders SET student_code='6935009999' WHERE order_code=?",
            ("FP2890041",),
        )
        temp_db.commit()
        rows = temp_db.execute(
            "SELECT * FROM orders WHERE student_code=? AND status IN ('shipped','refund','received')"
            " ORDER BY created_at DESC",
            ("6935009999",),
        ).fetchall()
        assert [r["order_code"] for r in rows] == ["FP2890041"]


class TestStationClaimedToggleMovesTheQueueRow:
    """The admin edit-form toggle has to move the Display 2 khantok row with it.

    Before this, unticking cleared khantok_station_claimed_at but left the person
    queued at the khantok station, and ticking never queued anyone — the checkbox
    was a label that did not control the queue.
    """

    def _ticketed(self, connection, code, *, station_claimed_at=None):
        _insert_summary_order(connection, code, status="received",
                              station_claimed_at=station_claimed_at,
                              received_at="2026-07-26T10:00:00+07:00")
        connection.execute(
            "UPDATE orders SET khantok_ticket=1, khantok_ticket_value=100 WHERE order_code=?",
            (code,),
        )
        return orders.fetch_order_by_code(connection, code)

    def _khantok_rows(self, connection, code):
        return connection.execute(
            "SELECT picked_at FROM display2_picks WHERE order_code=? AND item_index=-1",
            (code,),
        ).fetchall()

    def test_ticking_on_queues_the_khantok_row(self, temp_db):
        self._ticketed(temp_db, "FP2890051")
        orders.update_order_fields(temp_db, "FP2890051", {"khantokStationClaimed": True})
        assert len(self._khantok_rows(temp_db, "FP2890051")) == 1
        row = orders.fetch_order_by_code(temp_db, "FP2890051")
        assert row["khantok_station_claimed_at"]

    def test_ticking_on_repairs_a_stamped_order_with_no_queue_row(self, temp_db):
        # orders left inconsistent by the old behaviour: stamp set, nothing queued
        self._ticketed(temp_db, "FP2890052", station_claimed_at="2026-07-26T11:00:00+07:00")
        orders.update_order_fields(temp_db, "FP2890052", {"khantokStationClaimed": True})
        assert len(self._khantok_rows(temp_db, "FP2890052")) == 1

    def test_unticking_removes_an_unpicked_queue_row(self, temp_db):
        row = self._ticketed(temp_db, "FP2890053")
        display.enqueue_display2_khantok(temp_db, row, "staff1")
        assert len(self._khantok_rows(temp_db, "FP2890053")) == 1
        orders.update_order_fields(temp_db, "FP2890053", {"khantokStationClaimed": False})
        assert self._khantok_rows(temp_db, "FP2890053") == []
        assert orders.fetch_order_by_code(temp_db, "FP2890053")["khantok_station_claimed_at"] is None

    def test_unticking_keeps_a_row_the_station_already_picked(self, temp_db):
        row = self._ticketed(temp_db, "FP2890054")
        display.enqueue_display2_khantok(temp_db, row, "staff1")
        temp_db.execute(
            "UPDATE display2_picks SET picked_at=?, picked_by=? WHERE order_code=? AND item_index=-1",
            ("2026-07-26T12:00:00+07:00", "staff2", "FP2890054"),
        )
        orders.update_order_fields(temp_db, "FP2890054", {"khantokStationClaimed": False})
        rows = self._khantok_rows(temp_db, "FP2890054")
        assert len(rows) == 1 and rows[0]["picked_at"], "a picked ticket is a record of a real handout"
        assert orders.fetch_order_by_code(temp_db, "FP2890054")["khantok_station_claimed_at"] is None

    def test_rolling_status_back_off_received_clears_the_stamp(self, temp_db):
        row = self._ticketed(temp_db, "FP2890055", station_claimed_at="2026-07-26T11:00:00+07:00")
        display.enqueue_display2_khantok(temp_db, row, "staff1")
        orders.update_order_status(temp_db, "FP2890055", "shipped")
        assert self._khantok_rows(temp_db, "FP2890055") == []
        assert orders.fetch_order_by_code(temp_db, "FP2890055")["khantok_station_claimed_at"] is None
