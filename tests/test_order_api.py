"""Tests for shared time helpers retained by the order API."""
from core import timeutil


def test_now_iso_returns_iso_string():
    value = timeutil.now_iso()
    assert isinstance(value, str)
    assert "T" in value


def test_now_iso_has_timezone():
    value = timeutil.now_iso()
    assert value.endswith("Z") or "+" in value
