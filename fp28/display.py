"""Display 1 state and the Display 2 pick queue."""
from __future__ import annotations

import json

from .config import STATION_BY_SLUG
from .database import log_audit, open_db
from .timeutil import now_iso


def enqueue_display2(connection, order_row, claim_user):
    """Insert one display2_picks row per item in the order, mapped by product_slug.
    Idempotent on (order_internal_id, item_index). Returns count of new rows."""
    try:
        items = json.loads(order_row["items_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        items = []
    if not items:
        items = [{
            "slug": order_row["product_slug"],
            "name": order_row["product_name"],
            "size": order_row["size"],
            "quantity": order_row["quantity"],
        }]
    inserted = 0
    now = now_iso()
    for idx, item in enumerate(items):
        product = item.get("product") or {}
        slug = (item.get("slug") or product.get("slug") or "").lower()
        if not slug:
            cat = (product.get("category") or item.get("category") or "").lower()
            if cat == "shirt":
                slug = "single"
            elif cat in ("single", "jacket", "headband"):
                slug = cat
        station = STATION_BY_SLUG.get(slug)
        if not station:
            log_audit(connection, order_row["order_code"],
                      "display2_skipped", f"slug={slug} item={idx}")
            continue
        exists = connection.execute(
            "SELECT 1 FROM display2_picks WHERE order_internal_id=? AND item_index=?",
            (order_row["internal_id"], idx),
        ).fetchone()
        if exists:
            continue
        connection.execute(
            """INSERT INTO display2_picks
               (order_internal_id, order_code, item_index, station,
                product_slug, product_name, size, quantity,
                student_code, nickname, full_name, queued_at, queued_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_row["internal_id"], order_row["order_code"], idx, station,
             slug, item.get("name") or product.get("name", ""),
             item.get("size", ""), int(item.get("quantity", 1) or 1),
             order_row["student_code"], order_row["nickname"], order_row["full_name"],
             now, claim_user),
        )
        inserted += 1
        log_audit(connection, order_row["order_code"],
                  "display2_enqueued", f"station={station} item={idx}")

    return inserted


def enqueue_display2_khantok(connection, order_row, claim_user) -> int:
    """Insert the khantok-station pick row (item_index -1) for a ticketed order.
    Idempotent on (order_internal_id, -1). Returns 1 if a row was inserted, else 0."""
    row_keys = order_row.keys() if hasattr(order_row, "keys") else []
    has_kt = "khantok_ticket" in row_keys and int(order_row["khantok_ticket"] or 0) == 1
    if not has_kt:
        return 0
    kt_idx = -1
    exists_kt = connection.execute(
        "SELECT 1 FROM display2_picks WHERE order_internal_id=? AND item_index=?",
        (order_row["internal_id"], kt_idx),
    ).fetchone()
    if exists_kt:
        return 0
    kt_value = 0
    if "khantok_ticket_value" in row_keys:
        kt_value = int(order_row["khantok_ticket_value"] or 0)
    kt_size = f"฿{kt_value}" if kt_value else "บัตร"
    connection.execute(
        """INSERT INTO display2_picks
           (order_internal_id, order_code, item_index, station,
            product_slug, product_name, size, quantity,
            student_code, nickname, full_name, queued_at, queued_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (order_row["internal_id"], order_row["order_code"], kt_idx, "khantok",
         "khantok", "บัตรขันโตก", kt_size, 1,
         order_row["student_code"], order_row["nickname"], order_row["full_name"],
         now_iso(), claim_user),
    )
    log_audit(connection, order_row["order_code"],
              "display2_enqueued", f"station=khantok value={kt_value}")
    return 1


def display_state_set(username: str, payload: dict) -> None:
    with open_db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO display1_state (username, payload_json, updated_at) VALUES (?,?,?)",
            (username, json.dumps(payload, ensure_ascii=False), now_iso()),
        )


def display_state_get(username: str) -> dict:
    with open_db() as connection:
        row = connection.execute(
            "SELECT payload_json FROM display1_state WHERE username=?",
            (username,),
        ).fetchone()
    if row is None:
        return {"active": False}
    try:
        return json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return {"active": False}


def display_state_users() -> list[str]:
    with open_db() as connection:
        rows = connection.execute(
            "SELECT username FROM display1_state ORDER BY updated_at DESC"
        ).fetchall()
    return [r["username"] for r in rows]
