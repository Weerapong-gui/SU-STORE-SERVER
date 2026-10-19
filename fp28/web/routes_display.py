"""Display 1/2/3 screens and the static assets they load."""
from __future__ import annotations

import json
import re
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .. import config
from ..database import log_audit, open_db
from ..display import display_state_get, display_state_set, display_state_users
from ..pages import DISPLAY2_HTML, DISPLAY3_HTML, DISPLAY_HTML, FONT_PATH, TEMPLATES_DIR
from ..stats import create_orders_summary_cached
from ..timeutil import now_iso

if TYPE_CHECKING:
    from .handler import OrderRequestHandler

def handle_get_display1(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display1"""
    h.send_html(HTTPStatus.OK, DISPLAY_HTML)
    return


def handle_get_display2(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display2"""
    h.send_html(HTTPStatus.OK, DISPLAY2_HTML)
    return


def handle_get_dis3(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /dis3"""
    h.send_html(HTTPStatus.OK, DISPLAY3_HTML)
    return


def handle_get_dis3_data(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /dis3/data"""
    with open_db() as connection:
        summary = create_orders_summary_cached(connection)
    h.send_json(HTTPStatus.OK, {
        "polo": summary["qtySingle"],
        "jacket": summary["qtyJacket"],
    })
    return


def handle_get_assets_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    r"""GET /assets/(slides/[a-zA-Z0-9_.\-]+|[a-zA-Z0-9_.\-]+)"""
    _rel = match.group(1)
    _asset_file = config.SLIDES_DIR / _rel[7:] if _rel.startswith("slides/") else TEMPLATES_DIR / "assets" / _rel
    if _asset_file.exists():
        _ext = _asset_file.suffix.lower()
        _mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}.get(_ext.lstrip("."), "application/octet-stream")
        _data = _asset_file.read_bytes()
        h.send_response(HTTPStatus.OK)
        h.send_header("Content-Type", _mime)
        h.send_header("Content-Length", str(len(_data)))
        h.send_header("Cache-Control", "public, max-age=86400")
        h.end_headers()
        h.wfile.write(_data)
    else:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "not found"})
    return


def handle_get_fonts_sukhumvitset_ttc(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /fonts/SukhumvitSet.ttc"""
    if FONT_PATH.exists():
        data = FONT_PATH.read_bytes()
        h.send_response(HTTPStatus.OK)
        h.send_header("Content-Type", "font/ttf")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "public, max-age=86400")
        h.send_header("Access-Control-Allow-Origin", "*")
        h.end_headers()
        h.wfile.write(data)
    else:
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "font not found"})
    return


def handle_get_display1_users(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display1/users"""
    h.send_json(HTTPStatus.OK, display_state_users())
    return


def handle_get_display1_state(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display1/state"""
    from urllib.parse import parse_qs as _pqs
    _qs = _pqs(urlparse(h.path).query)
    user = (_qs.get("user") or [""])[0].strip()
    h.send_json(HTTPStatus.OK, display_state_get(user))
    return


def handle_get_display1_slides(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display1/slides"""
    _ok_exts = {".png", ".jpg", ".jpeg", ".webp"}
    _slide_files = sorted([f.name for f in config.SLIDES_DIR.iterdir() if f.is_file() and f.suffix.lower() in _ok_exts])
    h.send_json(HTTPStatus.OK, {"slides": [{"name": f, "url": f"/assets/slides/{f}"} for f in _slide_files]})
    return


def handle_get_display2_queue(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display2/queue"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    from urllib.parse import parse_qs as _pqs
    _qs = _pqs(urlparse(h.path).query)
    station = (_qs.get("station") or [""])[0].strip().lower()
    if station not in {"polo", "jacket", "headband", "khantok"}:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid station"})
        return
    with open_db() as connection:
        rows = connection.execute(
            """SELECT p.id, p.order_code, p.size, p.quantity, p.student_code,
                              p.nickname, p.full_name, p.queued_at, p.queued_by, p.undone_at,
                              p.item_index, o.items_json, o.status AS order_status
                         FROM display2_picks p
                         LEFT JOIN orders o ON o.internal_id = p.order_internal_id
                        WHERE p.station = ? AND p.picked_at IS NULL
                        ORDER BY p.queued_at DESC
                        LIMIT 50""",
            (station,),
        ).fetchall()
    items = []
    for r in rows:
        school = ""
        line_total = 0
        unit_price = 0
        if station == "headband":
            try:
                parsed = json.loads(r["items_json"] or "[]")
                if 0 <= r["item_index"] < len(parsed):
                    item = parsed[r["item_index"]] or {}
                    school = item.get("school") or ""
                    product = item.get("product") or {}
                    qty = int(item.get("quantity", 1) or 1)
                    unit_price = int(item.get("unitPrice") or product.get("price") or 35)
                    raw_total = item.get("totalAmount")
                    line_total = int(raw_total) if raw_total else unit_price * qty
            except (TypeError, json.JSONDecodeError, ValueError):
                pass
        items.append({
            "id": r["id"],
            "orderCode": r["order_code"],
            "size": r["size"],
            "quantity": r["quantity"],
            "studentCode": r["student_code"],
            "nickname": r["nickname"],
            "fullName": r["full_name"],
            "queuedAt": r["queued_at"],
            "queuedBy": r["queued_by"],
            "undone": r["undone_at"] is not None,
            "school": school,
            "unitPrice": unit_price,
            "lineTotal": line_total,
            "orderStatus": r["order_status"],
        })
    with open_db() as conn2:
        today_count = conn2.execute(
            """SELECT COUNT(*) AS c FROM display2_picks
                        WHERE station = ?
                          AND picked_at IS NOT NULL
                          AND DATE(picked_at, '+7 hours') = DATE('now', '+7 hours')""",
            (station,),
        ).fetchone()["c"]
    h.send_json(HTTPStatus.OK, {"items": items, "todayPicked": today_count})
    return


def handle_get_display2_assets_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display2/assets/([A-Za-z0-9_.-]+)"""
    fname = match.group(1)
    fpath = config.ASSETS_DIR / fname
    if not fpath.exists() or not fpath.is_file():
        h.send_json(HTTPStatus.NOT_FOUND, {"message": "asset not found"})
        return
    ext = fpath.suffix.lower()
    mime = {".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".webp":"image/webp",".gif":"image/gif"}.get(ext, "application/octet-stream")
    data = fpath.read_bytes()
    h.send_response(HTTPStatus.OK)
    h.send_header("Content-Type", mime)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "public, max-age=3600")
    h.end_headers()
    h.wfile.write(data)
    return


def handle_get_display2_admin_assets(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display2/admin/assets"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    with open_db() as connection:
        rows = connection.execute(
            "SELECT id, station, category, filename, uploaded_at, uploaded_by"
            " FROM display2_assets ORDER BY station, category, id"
        ).fetchall()
    grouped: dict = {"polo": [], "jacket": {}, "khantok": [], "headband": {}}
    for r in rows:
        entry = {
            "id": r["id"],
            "url": f"/display2/assets/{r['filename']}",
            "filename": r["filename"],
            "uploadedAt": r["uploaded_at"],
            "uploadedBy": r["uploaded_by"],
        }
        st = r["station"]
        if st in ("headband", "jacket"):
            cat = r["category"] or ""
            grouped[st].setdefault(cat, []).append(entry)
        elif st in grouped:
            grouped[st].append(entry)
    h.send_json(HTTPStatus.OK, grouped)
    return


def handle_get_display2_history(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """GET /display2/history"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    from urllib.parse import parse_qs as _pqs
    _qs = _pqs(urlparse(h.path).query)
    station = (_qs.get("station") or [""])[0].strip().lower()
    q = (_qs.get("q") or [""])[0].strip()
    if station not in {"polo", "jacket", "headband", "khantok"}:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid station"})
        return
    like = f"%{q}%"
    with open_db() as connection:
        rows = connection.execute(
            """SELECT id, order_code, size, nickname, student_code,
                              picked_at, picked_by, undone_at
                         FROM display2_picks
                        WHERE station = ?
                          AND picked_at IS NOT NULL
                          AND DATE(picked_at, '+7 hours') = DATE('now', '+7 hours')
                          AND (? = ''
                               OR order_code LIKE ?
                               OR student_code LIKE ?
                               OR nickname LIKE ?)
                        ORDER BY picked_at DESC
                        LIMIT 200""",
            (station, q, like, like, like),
        ).fetchall()
    items = [
        {
            "id": r["id"],
            "orderCode": r["order_code"],
            "size": r["size"],
            "nickname": r["nickname"],
            "studentCode": r["student_code"],
            "pickedAt": r["picked_at"],
            "pickedBy": r["picked_by"],
            "undone": r["undone_at"] is not None,
        }
        for r in rows
    ]
    h.send_json(HTTPStatus.OK, {"items": items})
    return


def handle_post_display1_update(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /display1/update"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    if isinstance(payload, dict):
        username = str(payload.get("username") or "").strip()
        if username:
            display_state_set(username, payload)
    h.send_json(HTTPStatus.OK, {"ok": True})
    return


def handle_post_display1_slides_upload(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /display1/slides/upload"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    _sname = re.sub(r"[^a-zA-Z0-9_.\-]", "_", str(payload.get("name", "")).strip())
    _sdata = str(payload.get("data", ""))
    if not _sname or not _sdata:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "missing name or data"})
        return
    if not re.search(r"\.(png|jpe?g|webp)$", _sname, re.IGNORECASE):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "unsupported type"})
        return
    import base64 as _b64
    try:
        _raw = _b64.b64decode(_sdata)
    except Exception:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid base64"})
        return
    if len(_raw) > 10 * 1024 * 1024:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "file too large (max 10MB)"})
        return
    (config.SLIDES_DIR / _sname).write_bytes(_raw)
    h.send_json(HTTPStatus.OK, {"ok": True, "url": f"/assets/slides/{_sname}"})
    return


def handle_post_display1_slides_delete(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /display1/slides/delete"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    _dname = str(payload.get("name", "")).strip()
    if not _dname or not re.fullmatch(r"[a-zA-Z0-9_.\-]+", _dname):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid name"})
        return
    _dtarget = config.SLIDES_DIR / _dname
    if _dtarget.exists():
        _dtarget.unlink()
    h.send_json(HTTPStatus.OK, {"ok": True})
    return


def handle_post_display2_pick(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /display2/pick"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    pick_id = (payload or {}).get("pick_id") if isinstance(payload, dict) else None
    if not isinstance(pick_id, int):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "pick_id required"})
        return
    picker = h.get_claim_station_user() or "staff"
    ts = now_iso()
    with open_db() as connection:
        cursor = connection.execute(
            "UPDATE display2_picks SET picked_at=?, picked_by=? "
            "WHERE id=? AND picked_at IS NULL",
            (ts, picker, pick_id),
        )
        if cursor.rowcount == 0:
            row = connection.execute(
                "SELECT picked_by, order_code, station FROM display2_picks WHERE id=?",
                (pick_id,),
            ).fetchone()
            if row is None:
                h.send_json(HTTPStatus.NOT_FOUND, {"message": "pick not found"})
            else:
                h.send_json(HTTPStatus.CONFLICT, {
                    "ok": False,
                    "reason": "already_picked",
                    "pickedBy": row["picked_by"] or "",
                })
            return
        row = connection.execute(
            "SELECT order_code, order_internal_id, station FROM display2_picks WHERE id=?",
            (pick_id,),
        ).fetchone()
        log_audit(connection, row["order_code"], "display2_picked",
                  f"station={row['station']} by={picker}")
        auto_refunded = False
        if row["station"] == "headband":
            order_row = connection.execute(
                "SELECT status, items_json FROM orders WHERE internal_id=?",
                (row["order_internal_id"],),
            ).fetchone()
            if order_row:
                try:
                    _items = json.loads(order_row["items_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    _items = []
                def _slug(it):
                    p = it.get("product") or {}
                    return (it.get("slug") or p.get("slug") or "").lower()
                headband_only = bool(_items) and all(
                    "headband" in _slug(i) for i in _items
                )
                if headband_only and order_row["status"] not in ("refunded", "cancelled", "rejected"):
                    remaining = connection.execute(
                        "SELECT COUNT(*) AS c FROM display2_picks "
                        "WHERE order_internal_id=? AND station='headband' AND picked_at IS NULL",
                        (row["order_internal_id"],),
                    ).fetchone()["c"]
                    if remaining == 0:
                        connection.execute(
                            "UPDATE orders SET status='refunded', payment_status='refunded', updated_at=?"
                            " WHERE internal_id=? AND status NOT IN ('refunded','cancelled','rejected')",
                            (ts, row["order_internal_id"]),
                        )
                        log_audit(connection, row["order_code"], "refund_completed",
                                  f"auto set by headband station by={picker} (prev={order_row['status']})")
                        auto_refunded = True
        connection.commit()
    resp = {"ok": True}
    if auto_refunded:
        resp["autoRefunded"] = True
    h.send_json(HTTPStatus.OK, resp)
    return


def handle_post_display2_undo(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /display2/undo"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    try:
        payload = h.read_json()
    except json.JSONDecodeError:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid json"})
        return
    pick_id = (payload or {}).get("pick_id") if isinstance(payload, dict) else None
    if not isinstance(pick_id, int):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "pick_id required"})
        return
    undoer = h.get_claim_station_user() or "staff"
    ts = now_iso()
    with open_db() as connection:
        cursor = connection.execute(
            """UPDATE display2_picks
                          SET undone_at=?, picked_at=NULL, picked_by=NULL
                        WHERE id=?
                          AND picked_at IS NOT NULL
                          AND julianday(picked_at) >= julianday('now', '-1 hour')""",
            (ts, pick_id),
        )
        if cursor.rowcount == 0:
            row = connection.execute(
                "SELECT picked_at FROM display2_picks WHERE id=?",
                (pick_id,),
            ).fetchone()
            if row is None:
                h.send_json(HTTPStatus.NOT_FOUND, {"message": "pick not found"})
            else:
                h.send_json(422, {"ok": False, "reason": "window_expired"})
            return
        row = connection.execute(
            "SELECT order_code, station FROM display2_picks WHERE id=?",
            (pick_id,),
        ).fetchone()
        log_audit(connection, row["order_code"], "display2_undone",
                  f"station={row['station']} by={undoer}")
        connection.commit()
    h.send_json(HTTPStatus.OK, {"ok": True})
    return


def handle_post_display2_admin_assets(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    """POST /display2/admin/assets"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    content_type = h.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "multipart/form-data required"})
        return
    content_length = int(h.headers.get("Content-Length", "0"))
    if content_length <= 0 or content_length > 10 * 1024 * 1024:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid size (max 10MB)"})
        return
    raw_body = h.rfile.read(content_length)
    message = BytesParser(policy=default).parsebytes(
        (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw_body
    )
    if not message.is_multipart():
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid multipart"})
        return
    station = ""
    category = ""
    img_bytes = b""
    img_ext = ".jpg"
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = str(part.get_param("name", header="content-disposition") or "")
        if name == "station":
            station = (part.get_payload(decode=True) or b"").decode("utf-8", "ignore").strip().lower()
        elif name == "category":
            category = (part.get_payload(decode=True) or b"").decode("utf-8", "ignore").strip()
        elif name == "image":
            img_bytes = part.get_payload(decode=True) or b""
            fn = str(part.get_filename() or "")
            ext = Path(fn).suffix.lower()
            if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                img_ext = ext
    if station not in {"polo", "jacket", "headband", "khantok"}:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "invalid station"})
        return
    if not img_bytes:
        h.send_json(HTTPStatus.BAD_REQUEST, {"message": "image required"})
        return
    uploader = h.get_claim_station_user() or "staff"
    import uuid as _uuid
    new_name = f"{station}_{_uuid.uuid4().hex[:12]}{img_ext}"
    (config.ASSETS_DIR / new_name).write_bytes(img_bytes)
    with open_db() as connection:
        cur = connection.execute(
            "INSERT INTO display2_assets (station, category, filename, uploaded_at, uploaded_by)"
            " VALUES (?,?,?,?,?)",
            (station, category or None, new_name, now_iso(), uploader),
        )
        connection.commit()
        asset_id = cur.lastrowid
    h.send_json(HTTPStatus.CREATED, {
        "id": asset_id, "url": f"/display2/assets/{new_name}",
        "filename": new_name, "station": station, "category": category or None,
    })
    return


def handle_delete_display2_admin_assets_by_id(h: OrderRequestHandler, path: str, match: re.Match[str] | None) -> None:
    r"""DELETE /display2/admin/assets/(\d+)"""
    if not h.has_claim_station_authorization():
        h.deny_unauthorized()
        return
    aid = int(match.group(1))
    with open_db() as connection:
        row = connection.execute("SELECT filename FROM display2_assets WHERE id=?", (aid,)).fetchone()
        if not row:
            h.send_json(HTTPStatus.NOT_FOUND, {"message": "not found"})
            return
        connection.execute("DELETE FROM display2_assets WHERE id=?", (aid,))
        connection.commit()
    try:
        fp = config.ASSETS_DIR / row["filename"]
        if fp.exists():
            fp.unlink()
    except Exception:
        pass
    h.send_json(HTTPStatus.OK, {"ok": True})
    return
