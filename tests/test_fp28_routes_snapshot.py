"""Characterization test for the FP28 (v1) HTTP API.

Runs one scripted session against a real OrderRequestHandler — public pages, every GET
route with each kind of auth, and the main write flows (order → slip → paid → received
→ display 2, users, products, settings, backups) — and compares every response plus the
final database contents with a stored snapshot.

It exists so order_api.py can be restructured with proof that behaviour did not change.
Timestamps, random tokens and epoch numbers are normalised; HTML/binary bodies are
compared by hash.

Regenerate (only when a behaviour change is intended):
    UPDATE_SNAPSHOT=1 pytest server/tests/test_fp28_routes_snapshot.py
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules.setdefault("ocr", MagicMock())

import order_api  # noqa: E402  (needs the ocr stub above)

SNAPSHOT = Path(__file__).parent / "snapshots" / "fp28_routes.json"
TOKEN = "test-token"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_HEX = re.compile(r"\b[0-9a-f]{16,}\b")
_EPOCH = re.compile(r"\b1\d{9}\b")
_tmp_dirs: list[str] = []  # the test's tmp_path, which differs per run


def _norm_text(text: str) -> str:
    for tmp in _tmp_dirs:
        text = text.replace(tmp, "<tmp>")
    return _EPOCH.sub("<epoch>", _HEX.sub("<hex>", _DATE.sub("<date>", _TS.sub("<ts>", text))))


def _modules() -> list[Any]:
    """order_api plus any module it was split into, so patches follow the code."""
    return [m for name, m in sys.modules.items() if m is not None and (name == "order_api" or name.startswith("fp28"))]


def _has(name: str) -> bool:
    return any(name in vars(m) for m in _modules())


def _find(name: str) -> Any:
    """`name` from wherever it lives now (order_api or an fp28 module)."""
    for m in _modules():
        if name in vars(m):
            return vars(m)[name]
    raise AttributeError(name)


def _patch(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    hits = [m for m in _modules() if name in vars(m)]
    assert hits, f"{name} not found in order_api or fp28.*"
    for m in hits:
        monkeypatch.setattr(m, name, value)


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_tmp_dirs", [str(tmp_path)])
    for name, value in {
        "DB_PATH": tmp_path / "orders.db",
        "SLIPS_DIR": tmp_path / "slips",
        "PRODUCT_IMAGES_DIR": tmp_path / "product-images",
        ("SLIDES_DIR" if _has("SLIDES_DIR") else "_slides_dir"): tmp_path / "slides",
        ("ASSETS_DIR" if _has("ASSETS_DIR") else "_assets_dir"): tmp_path / "assets",
        "ORDER_API_TOKEN": TOKEN,
        "trigger_ocr_async": lambda *a, **k: None,
        "_CONN_POOL": [],
    }.items():
        _patch(monkeypatch, name, value)
    for d in ("slips", "product-images", "slides", "assets"):
        (tmp_path / d).mkdir()
    _find("ensure_db")()
    with sqlite3.connect(tmp_path / "orders.db") as conn:
        conn.execute(
            "INSERT INTO admin_users (username, claim_token, super_token, is_superadmin, created_by, created_at) "
            "VALUES ('staff1', ?, NULL, 0, 'test', '2026-01-01T00:00:00+07:00'), "
            "('root', ?, ?, 1, 'test', '2026-01-01T00:00:00+07:00')",
            (
                _find("compute_claim_token")("staff1", "pw"),
                _find("compute_claim_token")("root", "pw"),
                _find("compute_super_token")("root", "pw"),
            ),
        )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), order_api.OrderRequestHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path / "orders.db"
    httpd.shutdown()


AUTH = {
    "none": {},
    "bearer": {"Authorization": f"Bearer {TOKEN}"},
    "claim": {"Authorization": "Claim " + _find("compute_claim_token")("staff1", "pw")},
    "super": {"Authorization": "Superadmin " + _find("compute_super_token")("root", "pw")},
}


class Session:
    def __init__(self, url: str) -> None:
        self.url = url
        self.log: list[dict[str, Any]] = []

    def call(self, method: str, path: str, auth: str = "none", body: Any = None, raw: bytes | None = None,
             content_type: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={**AUTH[auth], **(headers or {})})
        if data is not None:
            req.add_header("Content-Type", content_type or "application/json")
        try:
            with urllib.request.urlopen(req) as res:
                status, ctype, payload = res.status, res.headers.get("Content-Type", ""), res.read()
        except urllib.error.HTTPError as error:
            status, ctype, payload = error.code, error.headers.get("Content-Type", ""), error.read()
        parsed: Any
        if "json" in ctype:
            parsed = json.loads(payload)
            shown: Any = json.loads(_norm_text(json.dumps(parsed, ensure_ascii=False, sort_keys=True)))
        elif ctype.startswith("text/csv"):
            parsed = shown = _norm_text(payload.decode("utf-8-sig"))
        else:
            parsed = payload
            shown = {"sha256": hashlib.sha256(_norm_text(payload.decode("latin-1")).encode("latin-1")).hexdigest()[:16],
                     "bytes": len(payload)}
        self.log.append({"req": f"{method} {path} [{auth}]", "status": status, "type": ctype.split(";")[0], "body": shown})
        return status, parsed


ORDER_PRODUCT = {"slug": "single-shirt", "name": "FRESHER POLO SHIRT", "shortName": "เสื้อเดี่ยว",
                 "tagline": "t", "price": 399, "image": "/x.png", "category": "single"}
HEADBAND = {**ORDER_PRODUCT, "slug": "fresh-headband", "name": "HEADBAND", "price": 59, "category": "headband"}
CUSTOMER = {"studentCode": _find("KHANTOK_STUDENT_CODE_PREFIX") + "1234567", "email": "a@b.c", "fullName": "Somchai",
            "phone": "0812345678", "school": "IT", "parentPhone": "0899999999"}

GET_PATHS = [
    "/health", "/", "/admin", "/docs", "/openapi.yaml", "/admin/receipt-template", "/orders", "/claim-station",
    "/display1", "/display2", "/dis3", "/dis3/data", "/fonts/SukhumvitSet.ttc", "/display1/users",
    "/display1/state", "/display1/slides", "/display2/queue", "/display2/admin/assets", "/display2/history",
    "/claim-station/stats", "/claim-station/orders?q=Somchai", "/claim-station/orders-pending",
    "/check-order?studentCode=" + CUSTOMER["studentCode"], "/admin/orders", "/products", "/site-settings",
    "/site-status", "/admin/orders/export.csv", "/admin/product-breakdown", "/admin/analytics", "/admin/audit-log",
    "/admin/feedback", "/admin/site-settings", "/admin/backups", "/admin/users", "/product-images/missing.png",
    "/assets/missing.png", "/nope",
]


def _sweep(s: Session, auths: tuple[str, ...]) -> None:
    for path in GET_PATHS:
        for auth in auths:
            s.call("GET", path, auth)


def _scenario(s: Session) -> None:
    _sweep(s, ("none", "bearer", "claim", "super"))

    _, a = s.call("POST", "/orders", body={"product": ORDER_PRODUCT, "customer": CUSTOMER, "size": "M", "quantity": 1,
                                           "totalAmount": 399, "khantokTicket": True})
    _, b = s.call("POST", "/orders", body={"product": HEADBAND, "customer": {**CUSTOMER, "studentCode": "6400000001"},
                                           "size": "IT", "quantity": 1, "totalAmount": 59})
    code, token = a.get("orderId") or a.get("orderCode") or a.get("id"), a.get("accessToken", "")
    code_b = b.get("orderId") or b.get("orderCode") or b.get("id")
    s.call("POST", "/orders", body={"product": ORDER_PRODUCT})  # invalid
    s.call("GET", f"/orders/{code}")
    s.call("GET", f"/orders/{code}", headers={"X-Order-Token": token})
    slip = {"slip": {"originalName": "s.png", "mimeType": "image/png", "size": len(PNG), "uploadedAt": "2026-10-11T10:00:00+07:00",
                     "fileContentBase64": base64.b64encode(PNG).decode()}}
    s.call("PATCH", f"/orders/{code}/slip", body=slip, headers={"X-Order-Token": token})
    s.call("GET", f"/admin/orders/{code}/slip", "bearer")
    s.call("GET", f"/admin/orders/{code}/slip-check", "bearer")
    s.call("GET", f"/admin/orders/{code}/extra-slips", "bearer")
    boundary = "BOUNDARY"
    multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"slip\"; filename=\"e.png\"\r\n"
                 f"Content-Type: image/png\r\n\r\n").encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
    s.call("POST", f"/admin/orders/{code}/extra-slips", "bearer", raw=multipart,
           content_type=f"multipart/form-data; boundary={boundary}")
    s.call("GET", f"/admin/orders/{code}/extra-slips", "bearer")
    s.call("PATCH", f"/admin/orders/{code}/status", "bearer", body={"status": "paid"})
    s.call("PATCH", f"/admin/orders/{code}", "bearer", body={"adminNote": "checked"})
    s.call("PATCH", f"/claim-station/orders/{code}/name", "claim", body={"fullName": "Somchai J."})
    s.call("PATCH", f"/claim-station/orders/{code}/received", "claim", body={})  # not ready yet
    s.call("PATCH", f"/admin/orders/{code}/status", "bearer", body={"status": "shipped"})
    s.call("PATCH", f"/claim-station/orders/{code}/received", "claim", body={})
    s.call("PATCH", f"/claim-station/orders/{code}/khantok-claimed", "claim", body={})
    s.call("PATCH", f"/claim-station/orders/{code_b}/received", "claim", body={})
    s.call("GET", f"/claim-station/orders/{code}", "claim")
    s.call("GET", f"/claim-station/orders/{code}/slip", "claim")
    for station in ("polo", "headband", "khantok"):
        s.call("GET", f"/display2/queue?station={station}", "claim")
    s.call("POST", "/display2/pick", "claim", body={"pick_id": 1})
    s.call("GET", "/display2/history?station=polo", "claim")
    s.call("POST", "/display2/undo", "claim", body={"pick_id": 1})
    s.call("POST", "/display1/update", "claim", body={"orderCode": code, "state": "show"})
    s.call("POST", f"/orders/{code}/feedback", body={"rating": 5, "comment": "nice"}, headers={"X-Order-Token": token})
    s.call("PATCH", "/admin/orders/bulk-status", "bearer", body={"orderIds": [code_b], "status": "cancelled"})
    s.call("PUT", "/admin/products/single-shirt", "bearer", body={"price": 420, "name": "POLO"})
    s.call("PATCH", "/admin/products/fresh-jacket/available", "bearer", body={"available": False})
    s.call("PATCH", "/admin/site-settings", "bearer", body={"announcementBanner": "hi", "announcementBannerEnabled": True})
    s.call("POST", "/claim-station/login", body={"username": "staff1", "password": "pw"})
    s.call("POST", "/claim-station/login", body={"username": "staff1", "password": "nope"})
    s.call("POST", "/admin/superlogin", body={"username": "root", "password": "pw"})
    s.call("POST", "/admin/users", "super", body={"username": "newbie", "password": "pw2"})
    s.call("PUT", "/admin/users/newbie/password", "super", body={"password": "pw-3333"})
    s.call("DELETE", "/admin/users/newbie", "super")
    s.call("POST", "/admin/backups", "bearer", body={"label": "snap"})
    s.call("DELETE", "/admin/backups/1", "bearer")
    s.call("DELETE", f"/admin/orders/{code}/extra-slips/1", "bearer")
    s.call("DELETE", f"/admin/orders/{code}/slip", "bearer")
    s.call("PUT", f"/orders/{code}", body={"customer": CUSTOMER}, headers={"X-Order-Token": token})
    s.call("DELETE", "/nope")
    s.call("PUT", "/nope")
    s.call("PATCH", "/nope")

    _sweep(s, ("bearer", "claim"))


def _dump_db(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    out = {}
    for t in tables:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {t} LIMIT 0").description]
        out[t] = sorted(_norm_text(json.dumps(dict(zip(cols, r, strict=True)), ensure_ascii=False, sort_keys=True, default=str))
                        for r in rows)
    conn.close()
    return out


def test_fp28_routes_match_snapshot(base):
    url, db_path = base
    s = Session(url)
    _scenario(s)
    actual = {"responses": s.log, "db": _dump_db(db_path)}
    if os.environ.get("UPDATE_SNAPSHOT"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(actual, ensure_ascii=False, indent=1) + "\n")
    expected = json.loads(SNAPSHOT.read_text())
    for i, (got, want) in enumerate(zip(actual["responses"], expected["responses"], strict=True)):
        assert got == want, f"response #{i} differs: {want['req']}\n{_diff(want, got)}"
    assert actual["db"] == expected["db"], _diff(expected["db"], actual["db"])


def _diff(want: Any, got: Any) -> str:
    dump = lambda v: json.dumps(v, ensure_ascii=False, indent=1, sort_keys=True).splitlines()  # noqa: E731
    return "\n".join(difflib.unified_diff(dump(want), dump(got), "snapshot", "actual", lineterm="", n=1))


def test_admin_stats_stream_sends_first_event(base):
    """The SSE stream never ends, so the snapshot session can't call it; read one event."""
    import http.client

    url, _ = base
    host, port = url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.request("GET", "/admin/stats-stream", headers=AUTH["bearer"])
    res = conn.getresponse()
    assert res.status == 200 and res.getheader("Content-Type") == "text/event-stream"
    first = res.fp.readline()
    conn.close()
    assert first.startswith(b"data: ") and "visitors" in json.loads(first[6:])
