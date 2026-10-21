import pytest

from core import visitors

ID_A = "3f2b8c1e-9a4d-4e7b-8c2a-1d5e6f7a8b9c"
ID_B = "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"


@pytest.fixture(autouse=True)
def empty_registry():
    visitors._last_seen.clear()
    yield
    visitors._last_seen.clear()


def test_two_cookies_behind_one_ip_count_as_two():
    for vid in (ID_A, ID_B):
        visitors.record_visit(visitors.visitor_key(vid, "10.0.0.1"), now=1000)
    assert visitors.active_count(now=1000) == 2


def test_invalid_id_falls_back_to_ip():
    assert visitors.visitor_key("not-a-uuid", "10.0.0.1") == "ip:10.0.0.1"
    assert visitors.visitor_key(ID_A.upper(), "") == f"vid:{ID_A}"


def test_nothing_usable_is_ignored():
    assert visitors.visitor_key("", "") is None
    assert visitors.visitor_key("x" * 500, "evil") is None


def test_visits_expire():
    visitors.record_visit("vid:a", now=1000)
    assert visitors.active_count(now=1000 + visitors.ACTIVE_SECONDS + 1) == 0


def test_registry_is_capped(monkeypatch):
    monkeypatch.setattr(visitors, "MAX_TRACKED", 3)
    for i in range(3):
        visitors.record_visit(f"vid:{i}", now=1000)
    visitors.record_visit("vid:new", now=1000)
    assert "vid:new" not in visitors._last_seen
    # once the old ones expire there is room again
    later = 1000 + visitors.ACTIVE_SECONDS + 1
    visitors.record_visit("vid:new", now=later)
    assert visitors._last_seen.keys() == {"vid:new"}
