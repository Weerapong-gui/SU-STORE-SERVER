import json
import sqlite3
from datetime import datetime
from uuid import uuid4

import pytest
from core import config, visitors
from core.timeutil import TZ_BANGKOK, now_iso

from .test_store_v2 import _call, server  # noqa: F401 (import pytest fixture)


@pytest.fixture(autouse=True)
def empty_visitor_registry():
    visitors._last_seen.clear()
    yield
    visitors._last_seen.clear()


def _set_settings(**values):
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO site_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, value, now_iso()) for key, value in values.items()],
        )


def _today_bangkok():
    return datetime.now(TZ_BANGKOK).strftime("%Y-%m-%d")


def _get_json(server, path):  # noqa: F811 (imported pytest fixture)
    status, body = _call(server, "GET", path)
    return status, json.loads(body)


def test_site_status_ignores_fp28_schedule(server):  # noqa: F811 (imported pytest fixture)
    _set_settings(
        schedule_enabled="1",
        be_right_back_active="1",
        be_right_back_dates=_today_bangkok(),
        site_closed="0",
    )
    status, body = _get_json(server, "/site-status?vid=" + str(uuid4()))
    assert status == 200
    assert body == {
        "siteClosed": False,
        "scheduleEnabled": False,
        "scheduleClosed": False,
        "scheduleWarning": False,
        "scheduleWarningMessage": "",
        "activeVisitors": 1,
        "beRightBack": False,
    }


def test_site_status_reports_site_closed(server):  # noqa: F811 (imported pytest fixture)
    _set_settings(site_closed="1")
    assert _get_json(server, "/site-status")[1]["siteClosed"] is True


def test_health(server):  # noqa: F811 (imported pytest fixture)
    assert _get_json(server, "/health") == (200, {"status": "ok"})


def test_product_image_rejects_path_tricks(server):  # noqa: F811 (imported pytest fixture)
    assert _call(server, "GET", "/product-images/..%2Fdb")[0] == 400
    assert _call(server, "GET", "/product-images/missing.jpg")[0] == 404
