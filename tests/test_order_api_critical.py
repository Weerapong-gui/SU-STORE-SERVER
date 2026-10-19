"""DB-backed tests for the money/quota-critical paths in order_api.py.

Covers previously-untested logic:
  - reserve_khantok_ticket: prefix gate, one-ticket-per-student de-dup (and the fix that
    a cancelled/rejected/refunded prior order must NOT block a re-claim), 100→50 quota
    rollover, quota-full denial, and idempotent re-claim of an existing order.
  - validate_order_payload: required-field rejection and the server-side total recompute
    that ignores a client-supplied top-level totalAmount.
"""
import os
import sqlite3
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Stub OCR so order_api imports without cv2/Tesseract present.
sys.modules["ocr"] = MagicMock()

from fp28 import config, khantok, orders  # noqa: E402


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE orders (internal_id INTEGER PRIMARY KEY, student_code TEXT, "
        "khantok_ticket INTEGER DEFAULT 0, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE khantok_ticket_claims (order_id INTEGER PRIMARY KEY, "
        "claimed_at TEXT, ticket_value INTEGER)"
    )
    return conn


def _add_order(conn, internal_id, student_code, khantok_ticket=0, status="pending_payment"):
    conn.execute(
        "INSERT INTO orders (internal_id, student_code, khantok_ticket, status) VALUES (?, ?, ?, ?)",
        (internal_id, student_code, khantok_ticket, status),
    )


ELIGIBLE = config.KHANTOK_STUDENT_CODE_PREFIX + "1234567"
INELIGIBLE = "641234567"


class TestReserveKhantokTicket:
    def test_non_prefix_gets_nothing(self):
        conn = _mem_db()
        _add_order(conn, 1, INELIGIBLE)
        ok, _claimed, already, value = khantok.reserve_khantok_ticket(conn, 1, INELIGIBLE)
        assert (ok, already, value) == (False, False, None)

    def test_eligible_prefix_gets_100_and_records_claim(self):
        conn = _mem_db()
        _add_order(conn, 1, ELIGIBLE)
        ok, _claimed, already, value = khantok.reserve_khantok_ticket(conn, 1, ELIGIBLE)
        assert ok is True and already is False and value == 100
        assert conn.execute("SELECT COUNT(*) AS c FROM khantok_ticket_claims").fetchone()["c"] == 1

    def test_second_live_order_for_same_student_is_blocked(self):
        conn = _mem_db()
        _add_order(conn, 1, ELIGIBLE, khantok_ticket=1, status="paid")   # already has a live ticket
        _add_order(conn, 2, ELIGIBLE, status="pending_payment")
        ok, _claimed, already, value = khantok.reserve_khantok_ticket(conn, 2, ELIGIBLE)
        assert (ok, already, value) == (False, True, None)

    def test_cancelled_prior_order_does_not_block_reclaim(self):
        """Regression guard: a ticketed order that was cancelled/rejected/refunded must not
        permanently deny the student a ticket on a brand-new order."""
        for dead in ("cancelled", "rejected", "refund", "refunded"):
            conn2 = _mem_db()
            _add_order(conn2, 1, ELIGIBLE, khantok_ticket=1, status=dead)
            _add_order(conn2, 2, ELIGIBLE, status="pending_payment")
            ok, _claimed, already, value = khantok.reserve_khantok_ticket(conn2, 2, ELIGIBLE)
            assert ok is True and already is False and value == 100, f"status {dead} should not block"

    def test_quota_rollover_100_to_50(self, monkeypatch):
        conn = _mem_db()
        monkeypatch.setattr(config, "KHANTOK_QUOTA_100", 0)
        monkeypatch.setattr(config, "KHANTOK_QUOTA_50", 5)
        _add_order(conn, 1, ELIGIBLE)
        ok, _claimed, _already, value = khantok.reserve_khantok_ticket(conn, 1, ELIGIBLE)
        assert ok is True and value == 50

    def test_both_quotas_full_denies(self, monkeypatch):
        conn = _mem_db()
        monkeypatch.setattr(config, "KHANTOK_QUOTA_100", 0)
        monkeypatch.setattr(config, "KHANTOK_QUOTA_50", 0)
        _add_order(conn, 1, ELIGIBLE)
        ok, _claimed, already, value = khantok.reserve_khantok_ticket(conn, 1, ELIGIBLE)
        assert (ok, already, value) == (False, False, None)

    def test_existing_claim_is_idempotent(self):
        conn = _mem_db()
        _add_order(conn, 1, ELIGIBLE, khantok_ticket=1, status="paid")
        conn.execute(
            "INSERT INTO khantok_ticket_claims (order_id, claimed_at, ticket_value) VALUES (1, ?, 50)",
            ("2026-01-01T00:00:00Z",),
        )
        ok, claimed, already, value = khantok.reserve_khantok_ticket(conn, 1, ELIGIBLE)
        assert ok is True and already is False and value == 50
        assert claimed == "2026-01-01T00:00:00Z"
        # no duplicate claim row created
        assert conn.execute("SELECT COUNT(*) AS c FROM khantok_ticket_claims").fetchone()["c"] == 1


def _product(slug="single-shirt", price=158, category="single"):
    return {
        "slug": slug, "name": "n", "shortName": "s", "tagline": "t",
        "price": price, "image": "i", "category": category,
    }


def _customer():
    return {
        "studentCode": ELIGIBLE, "email": "a@b.c", "fullName": "N",
        "phone": "0800000000", "school": "X", "parentPhone": "0810000000",
    }


class TestValidateOrderPayload:
    def test_rejects_non_dict(self):
        data, err = orders.validate_order_payload("nope")
        assert data is None and err

    def test_rejects_missing_customer_field(self):
        payload = {
            "product": _product(), "customer": {**_customer(), "studentCode": ""},
            "size": "M", "quantity": 1, "totalAmount": 158,
        }
        data, err = orders.validate_order_payload(payload)
        assert data is None and "studentCode" in err

    def test_rejects_zero_quantity(self):
        payload = {
            "product": _product(), "customer": _customer(),
            "size": "M", "quantity": 0, "totalAmount": 158,
        }
        data, _err = orders.validate_order_payload(payload)
        assert data is None

    def test_total_is_recomputed_from_single_item(self):
        # Client sends a bogus top-level total; server derives it from unitPrice * qty.
        payload = {
            "product": _product(price=158), "customer": _customer(),
            "size": "M", "quantity": 2, "totalAmount": 999,
        }
        data, err = orders.validate_order_payload(payload)
        assert err is None
        assert data["total_amount"] == 316  # 158 * 2, not 999

    def test_total_is_recomputed_from_multi_items(self):
        items = [
            {"product": _product("single-shirt", 158), "size": "M", "quantity": 1, "unitPrice": 158},
            {"product": _product("fresh-jacket", 739, "jacket"), "size": "L", "quantity": 2, "unitPrice": 739},
        ]
        payload = {
            "product": _product(), "customer": _customer(),
            "size": "M", "quantity": 1, "totalAmount": 1, "items": items,
        }
        data, err = orders.validate_order_payload(payload)
        assert err is None
        assert data["total_amount"] == 158 + 739 * 2  # 1636, not the client's 1
        assert len(data["items"]) == 2
