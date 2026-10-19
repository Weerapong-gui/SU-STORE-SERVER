"""FP28 orders: validation, creation, updates and serialisation."""
from __future__ import annotations

import contextlib
import json
import sqlite3
from http import HTTPStatus
from typing import Any

from .auth import compute_qr_token, create_order_access_token
from .config import DEFAULT_PRODUCTS, ORDER_PREFIX, PAYMENT_STATUS_BY_ORDER_STATUS
from .database import log_audit, open_db
from .display import enqueue_display2_khantok
from .khantok import khantok_claim_warning, reserve_khantok_ticket
from .site import get_current_phase
from .slips import delete_local_slip_file, resolve_stored_slip_path
from .timeutil import now_iso


def mark_khantok_station_claimed(
    connection: sqlite3.Connection, order_code: str, claim_user: str
) -> tuple[int, dict[str, Any]]:
    """Mark a ticketed order's khantok ticket as claimed at the claim station and
    enqueue its khantok pick row. Never touches khantok_ticket_claims (quota).
    Returns (http_status, response_body). Does not commit."""
    existing = fetch_order_by_code(connection, order_code)
    if existing is None:
        return HTTPStatus.NOT_FOUND, {"message": "ไม่พบออเดอร์"}
    row_keys = existing.keys()
    has_ticket = "khantok_ticket" in row_keys and int(existing["khantok_ticket"] or 0) == 1
    if not has_ticket:
        return HTTPStatus.BAD_REQUEST, {"message": "ออเดอร์นี้ไม่มีบัตรขันโตก"}
    if existing["status"] in ("pending_payment", "waiting_confirm", "cancelled", "rejected"):
        return HTTPStatus.BAD_REQUEST, {
            "message": f"ออเดอร์มีสถานะ '{existing['status']}' ยังไม่พร้อมรับบัตร"
        }
    already = (
        existing["khantok_station_claimed_at"]
        if "khantok_station_claimed_at" in row_keys else None
    )
    if already:
        return HTTPStatus.CONFLICT, {
            "message": "รับบัตรขันโตกไปแล้ว", "claimedAt": already, "alreadyClaimed": True,
        }
    ts = now_iso()
    connection.execute(
        "UPDATE orders SET khantok_station_claimed_at=?, updated_at=? WHERE order_code=?",
        (ts, ts, order_code),
    )
    log_audit(connection, order_code, "khantok_claimed", f"claimed by {claim_user}")
    updated_row = fetch_order_by_code(connection, order_code)
    queued = 0
    try:
        queued = enqueue_display2_khantok(connection, updated_row, claim_user)
    except Exception as exc:
        log_audit(connection, order_code,
                  "display2_enqueue_failed", f"{type(exc).__name__}: {exc}")
    return HTTPStatus.OK, {"order": serialize_order(updated_row), "queued": queued}


def create_order_code(sequence_number: int, connection: sqlite3.Connection | None = None) -> str:
    return f"{ORDER_PREFIX}{sequence_number:04d}{get_current_phase(connection)}"


def has_feedback_for_order(order_code: str) -> bool:
    try:
        with open_db() as conn:
            row = conn.execute("SELECT 1 FROM order_feedback WHERE order_code=?", (order_code,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def serialize_order(row: sqlite3.Row, include_access_token: bool = False) -> dict[str, Any]:
    row_keys = set(row.keys())
    slip = None
    if row["slip_stored_name"]:
        slip = {
            "originalName": row["slip_original_name"],
            "storedName": row["slip_stored_name"],
            "storedPath": row["slip_storage_path"] or str(resolve_stored_slip_path(row["slip_stored_name"])),
            "mimeType": row["slip_mime_type"],
            "size": row["slip_size"],
            "uploadedAt": row["slip_uploaded_at"],
        }
    items: list[dict[str, Any]] = []
    if "items_json" in row_keys and row["items_json"]:
        try:
            parsed_items = json.loads(row["items_json"])
            if isinstance(parsed_items, list):
                items = [item for item in parsed_items if isinstance(item, dict)]
        except json.JSONDecodeError:
            items = []

    if not items:
        items = [
            {
                "id": row["product_slug"],
                "product": {
                    "slug": row["product_slug"],
                    "name": row["product_name"],
                    "shortName": row["product_short_name"],
                    "tagline": row["product_tagline"],
                    "price": row["product_price"],
                    "image": row["product_image"],
                    "category": row["product_category"],
                },
                "size": row["size"],
                "quantity": row["quantity"],
                "unitPrice": row["product_price"],
                "totalAmount": row["product_price"] * row["quantity"],
            }
        ]

    payload = {
        "id": row["order_code"],
        "sequenceNumber": row["internal_id"],
        "roundNumber": row["round_number"],
        "status": row["status"],
        "paymentStatus": row["payment_status"],
        "khantokTicket": bool(
            row["khantok_ticket"] if "khantok_ticket" in row_keys else row["lucky_ticket"]
        ),
        "khantokTicketClaimedAt": (
            row["khantok_ticket_claimed_at"]
            if "khantok_ticket_claimed_at" in row_keys
            else row["lucky_ticket_claimed_at"]
        ),
        "khantokTicketAlreadyClaimed": bool(row["khantok_ticket_already_claimed"]) if "khantok_ticket_already_claimed" in row_keys else False,
        "khantokStationClaimedAt": (
            row["khantok_station_claimed_at"]
            if "khantok_station_claimed_at" in row_keys
            else None
        ),
        "khantokClaimWarning": khantok_claim_warning(
            row["received_at"] if "received_at" in row_keys else None,
            row["status"] if "status" in row_keys else None,
        ),
        "khantokTicketValue": int(row["khantok_ticket_value"]) if "khantok_ticket_value" in row_keys and row["khantok_ticket_value"] is not None else None,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "size": row["size"],
        "quantity": row["quantity"],
        "totalAmount": row["total_amount"],
        "product": {
            "slug": row["product_slug"],
            "name": row["product_name"],
            "shortName": row["product_short_name"],
            "tagline": row["product_tagline"],
            "price": row["product_price"],
            "image": row["product_image"],
            "category": row["product_category"],
        },
        "items": items,
        "customer": {
            "studentCode": row["student_code"] or "",
            "email": row["email"],
            "fullName": row["full_name"] or " ".join(
                value for value in [row["first_name"], row["last_name"]] if value
            ).strip(),
            "phone": row["phone"],
            "school": row["school"],
            "parentPhone": row["parent_phone"] or "",
        },
        "slip": slip,
        "adminNote": row["admin_note"] if "admin_note" in row_keys else None,
        "receivedAt": row["received_at"] if "received_at" in row_keys else None,
        "receivedBy": row["received_by"] if "received_by" in row_keys else None,
        "feedbackSubmitted": has_feedback_for_order(row["order_code"]),
        "qrToken": compute_qr_token(row["order_code"]),
    }
    if include_access_token:
        payload["accessToken"] = row["access_token"]
    return payload


def validate_order_payload(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "payload ต้องเป็น JSON object"

    product = payload.get("product")
    customer = payload.get("customer")
    if not isinstance(product, dict) or not isinstance(customer, dict):
        return None, "payload ต้องมี product และ customer"

    required_product_fields = ["slug", "name", "shortName", "tagline", "price", "image", "category"]
    for field in required_product_fields:
        if field not in product or product[field] in (None, ""):
            return None, f"product.{field} is required"

    required_customer_fields = [
        "studentCode",
        "email",
        "fullName",
        "phone",
        "school",
        "parentPhone",
    ]
    for field in required_customer_fields:
        if field not in customer or customer[field] in (None, ""):
            return None, f"customer.{field} is required"

    size = payload.get("size")
    quantity = payload.get("quantity")
    total_amount = payload.get("totalAmount")

    if not isinstance(size, str) or not size.strip():
        return None, "size is required"
    if not isinstance(quantity, int) or quantity < 1:
        return None, "quantity must be a positive integer"
    if not isinstance(total_amount, int) or total_amount < 0:
        return None, "totalAmount must be a non-negative integer"

    raw_items = payload.get("items")
    normalized_items: list[dict[str, Any]] = []
    if isinstance(raw_items, list) and raw_items:
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                return None, "items must contain objects"

            item_product = item.get("product")
            if not isinstance(item_product, dict):
                return None, "item.product is required"

            for field in required_product_fields:
                if field not in item_product or item_product[field] in (None, ""):
                    return None, f"items[{index}].product.{field} is required"

            item_size = item.get("size")
            item_quantity = item.get("quantity")
            item_unit_price = item.get("unitPrice", item_product.get("price"))
            item_total_amount = item.get("totalAmount")

            if not isinstance(item_size, str) or not item_size.strip():
                return None, f"items[{index}].size is required"
            if not isinstance(item_quantity, int) or item_quantity < 1:
                return None, f"items[{index}].quantity must be a positive integer"
            if not isinstance(item_unit_price, int) or item_unit_price < 0:
                return None, f"items[{index}].unitPrice must be a non-negative integer"
            if not isinstance(item_total_amount, int):
                item_total_amount = item_unit_price * item_quantity

            item_school = item.get("school")
            normalized_items.append(
                {
                    "id": str(item.get("id") or f"{item_product.get('slug', 'item')}-{index + 1}"),
                    "product": item_product,
                    "size": item_size.strip(),
                    "school": item_school if isinstance(item_school, str) and item_school.strip() else None,
                    "quantity": item_quantity,
                    "unitPrice": item_unit_price,
                    "totalAmount": item_total_amount,
                }
            )

    if not normalized_items:
        normalized_items = [
            {
                "id": str(product.get("slug") or "item-1"),
                "product": product,
                "size": size.strip(),
                "quantity": quantity,
                "unitPrice": int(product["price"]),
                "totalAmount": int(product["price"]) * quantity,
            }
        ]

    # Derive the order total from the (validated) line items rather than trusting the
    # client-supplied top-level totalAmount. This guarantees the stored total always
    # matches the sum of items — the value OCR later checks the slip against.
    # NOTE: per-item unitPrice is still client-supplied; making price fully authoritative
    # requires a single source of product-price truth (see audit follow-up).
    computed_total = sum(int(item["totalAmount"]) for item in normalized_items)

    return {
        "product": product,
        "customer": customer,
        "size": size.strip(),
        "quantity": quantity,
        "total_amount": computed_total,
        "items": normalized_items,
    }, None


def validate_slip_payload(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "payload ต้องเป็น JSON object"
    slip = payload.get("slip")
    if not isinstance(slip, dict):
        return None, "payload ต้องมี slip"

    required_fields = ["originalName", "mimeType", "size", "uploadedAt"]
    for field in required_fields:
        if field not in slip or slip[field] in (None, ""):
            return None, f"slip.{field} is required"

    if (
        ("storedName" not in slip or slip["storedName"] in (None, ""))
        and ("storedPath" not in slip or slip["storedPath"] in (None, ""))
        and ("fileContentBase64" not in slip or slip["fileContentBase64"] in (None, ""))
    ):
        return None, "slip.storedName, slip.storedPath or slip.fileContentBase64 is required"

    return slip, None


def fetch_order_by_code(connection: sqlite3.Connection, order_code: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM orders WHERE order_code = ?",
        (order_code,),
    ).fetchone()


def list_orders(
    connection: sqlite3.Connection,
    *,
    page: int = 1,
    per_page: int = 50,
    search: str = "",
    status_filter: str = "",
    round_filter: int = 0,
    student_code_filter: str = "",
) -> tuple[list[dict[str, Any]], int]:
    conditions: list[str] = []
    params: list[Any] = []
    if round_filter > 0:
        conditions.append("round_number = ?")
        params.append(round_filter)
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)
    if student_code_filter:
        conditions.append("student_code LIKE ?")
        params.append(f"%{student_code_filter}%")
    if search:
        like = f"%{search}%"
        conditions.append(
            "(order_code LIKE ? OR full_name LIKE ? OR student_code LIKE ? OR phone LIKE ? OR school LIKE ? OR product_name LIKE ?)"
        )
        params.extend([like] * 6)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    total_row = connection.execute(f"SELECT COUNT(*) AS cnt FROM orders {where}", params).fetchone()
    total = int(total_row["cnt"] if total_row else 0)
    offset = (page - 1) * per_page
    rows = connection.execute(
        f"SELECT * FROM orders {where} ORDER BY internal_id DESC LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    ).fetchall()
    return [serialize_order(row) for row in rows], total


def update_order_status(
    connection: sqlite3.Connection,
    order_code: str,
    status: str,
) -> dict[str, Any] | None:
    existing_order = fetch_order_by_code(connection, order_code)
    if existing_order is None:
        return None

    prev_status = existing_order["status"]
    payment_status = PAYMENT_STATUS_BY_ORDER_STATUS[status]
    connection.execute(
        """
        UPDATE orders
        SET status = ?, payment_status = ?, updated_at = ?
        WHERE order_code = ?
        """,
        (status, payment_status, now_iso(), order_code),
    )
    log_audit(connection, order_code, "status_changed", f"{prev_status} → {status}")
    if prev_status == "received" and status != "received":
        removed = connection.execute(
            "DELETE FROM display2_picks WHERE order_internal_id=?",
            (existing_order["internal_id"],),
        ).rowcount
        if removed:
            log_audit(connection, order_code, "display2_cleared",
                      f"rollback received→{status}, removed {removed} picks")
        # The khantok queue row went with the rest, so the stamp claiming it was
        # handed out at the station has to go too — otherwise the order shows
        # "รับบัตรแล้ว" with nothing queued and no way back but the edit form.
        connection.execute(
            "UPDATE orders SET khantok_station_claimed_at = NULL WHERE order_code = ?",
            (order_code,),
        )
    row = fetch_order_by_code(connection, order_code)
    assert row is not None
    return serialize_order(row)


def update_order_fields(
    connection: sqlite3.Connection,
    order_code: str,
    fields: dict[str, Any],
    expected_updated_at: str | None = None,
) -> dict[str, Any] | bool | None:
    existing = fetch_order_by_code(connection, order_code)
    if existing is None:
        return None
    if expected_updated_at:
        existing_updated = existing["updated_at"] if "updated_at" in existing.keys() else ""  # noqa: SIM118 (sqlite3.Row: `in` checks values)
        if str(existing_updated or "") != str(expected_updated_at):
            return False  # conflict — order was modified by someone else

    allowed: dict[str, Any] = {}
    str_fields = {
        "fullName": "full_name",
        "studentCode": "student_code",
        "phone": "phone",
        "parentPhone": "parent_phone",
        "school": "school",
        "email": "email",
        "nickname": "nickname",
    }
    for key, col in str_fields.items():
        if key in fields and isinstance(fields[key], str):
            allowed[col] = fields[key].strip()

    if "adminNote" in fields:
        note = fields["adminNote"]
        allowed["admin_note"] = note.strip() if isinstance(note, str) else None

    # Khantok ticket — always sync khantok_ticket_claims together with orders fields
    khantok_changed = "khantokTicket" in fields and isinstance(fields["khantokTicket"], bool)
    value_changed = "khantokTicketValue" in fields
    claimed_changed = "khantokTicketAlreadyClaimed" in fields and isinstance(fields["khantokTicketAlreadyClaimed"], bool)

    if khantok_changed or value_changed or claimed_changed:
        order_id = existing["internal_id"]
        old_has_ticket = bool(existing["khantok_ticket"])
        new_has_ticket = fields["khantokTicket"] if khantok_changed else old_has_ticket

        if khantok_changed:
            allowed["khantok_ticket"] = 1 if new_has_ticket else 0
        if claimed_changed:
            allowed["khantok_ticket_already_claimed"] = 1 if fields["khantokTicketAlreadyClaimed"] else 0

        if not old_has_ticket and new_has_ticket:
            # Adding ticket: insert claim record so quota is consumed
            raw_val = fields.get("khantokTicketValue")
            new_value = int(raw_val) if isinstance(raw_val, (int, float)) and raw_val is not None else 100
            claimed_at = now_iso()
            connection.execute(
                "INSERT OR REPLACE INTO khantok_ticket_claims (order_id, claimed_at, ticket_value) VALUES (?, ?, ?)",
                (order_id, claimed_at, new_value),
            )
            allowed["khantok_ticket_value"] = new_value
            allowed["khantok_ticket_claimed_at"] = claimed_at

        elif old_has_ticket and not new_has_ticket:
            # Removing ticket: delete claim record so quota is returned
            connection.execute("DELETE FROM khantok_ticket_claims WHERE order_id = ?", (order_id,))
            allowed["khantok_ticket_value"] = None
            allowed["khantok_ticket_claimed_at"] = None

        elif old_has_ticket and new_has_ticket and value_changed:
            # Ticket stays but value changed: update the claim record's ticket_value
            raw_val = fields["khantokTicketValue"]
            new_value = int(raw_val) if isinstance(raw_val, (int, float)) and raw_val is not None else None
            if new_value is not None:
                connection.execute(
                    "UPDATE khantok_ticket_claims SET ticket_value = ? WHERE order_id = ?",
                    (new_value, order_id),
                )
            allowed["khantok_ticket_value"] = new_value

    # Station claim state — independent of quota; never touches khantok_ticket_claims
    # The toggle has to move the Display 2 khantok queue row as well as the column.
    # Editing only the column left the two out of step: unticking cleared the stamp
    # while the person stayed queued at the khantok station, and ticking never
    # queued anyone. "enqueue" runs even when the stamp is already set, so an order
    # left inconsistent by the old behaviour is repaired by re-ticking it.
    station_claim_action: str | None = None
    if "khantokStationClaimed" in fields and isinstance(fields["khantokStationClaimed"], bool):
        existing_station_claimed = (
            existing["khantok_station_claimed_at"]
            if "khantok_station_claimed_at" in existing.keys() else None  # noqa: SIM118 (sqlite3.Row)
        )
        if fields["khantokStationClaimed"]:
            station_claim_action = "enqueue"
            if not existing_station_claimed:
                allowed["khantok_station_claimed_at"] = now_iso()
        else:
            station_claim_action = "dequeue"
            allowed["khantok_station_claimed_at"] = None

    if "items" in fields and isinstance(fields["items"], list) and fields["items"]:
        raw_items = fields["items"]
        product_price_map: dict[str, int] = {}
        try:
            for prow in connection.execute("SELECT slug, price FROM products").fetchall():
                product_price_map[prow["slug"]] = int(prow["price"] or 0)
        except sqlite3.Error:
            pass
        for dp in DEFAULT_PRODUCTS:
            product_price_map.setdefault(dp["slug"], int(dp["price"]))
        new_items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            school = item.get("school")
            product = item.get("product") or {}
            slug = product.get("slug") or ""
            fallback_price = product_price_map.get(slug, 0)
            unit_price = int(item.get("unitPrice") or product.get("price") or fallback_price or 0)
            if not product.get("price") and fallback_price:
                product = {**product, "price": fallback_price}
            qty = max(1, int(item.get("quantity") or 1))
            total_amount = int(item.get("totalAmount") or (unit_price * qty))
            new_items.append({
                "id": str(item.get("id") or "item"),
                "product": product,
                "size": str(item.get("size") or "").strip(),
                "school": school if isinstance(school, str) and school.strip() else None,
                "quantity": qty,
                "unitPrice": unit_price,
                "totalAmount": total_amount,
            })
        if new_items:
            allowed["items_json"] = json.dumps(new_items, ensure_ascii=False)
            total = sum(it["totalAmount"] for it in new_items)
            allowed["total_amount"] = total
            first = new_items[0]
            allowed["size"] = first["size"]
            allowed["quantity"] = first["quantity"]

    if station_claim_action == "enqueue":
        enqueue_display2_khantok(connection, existing, "admin (edit form)")
    elif station_claim_action == "dequeue":
        # A row the khantok station already picked is a record that the ticket
        # physically went out — clearing the admin checkbox must not erase it.
        removed = connection.execute(
            "DELETE FROM display2_picks "
            "WHERE order_internal_id=? AND item_index=-1 AND picked_at IS NULL",
            (existing["internal_id"],),
        ).rowcount
        if removed:
            log_audit(connection, order_code, "display2_khantok_dequeued",
                      "admin edit form cleared the station-claimed toggle")
        else:
            still_picked = connection.execute(
                "SELECT 1 FROM display2_picks "
                "WHERE order_internal_id=? AND item_index=-1 AND picked_at IS NOT NULL",
                (existing["internal_id"],),
            ).fetchone()
            if still_picked:
                log_audit(connection, order_code, "display2_khantok_kept_picked",
                          "toggle cleared but the khantok row was already picked — row kept")

    if not allowed:
        return serialize_order(existing)

    allowed["updated_at"] = now_iso()
    set_clause = ", ".join(f"{col} = ?" for col in allowed)
    values = [*allowed.values(), order_code]
    connection.execute(f"UPDATE orders SET {set_clause} WHERE order_code = ?", values)

    changed = ", ".join(f"{k}={v!r}" for k, v in fields.items())
    log_audit(connection, order_code, "order_fields_updated", changed)
    row = fetch_order_by_code(connection, order_code)
    assert row is not None
    return serialize_order(row)


def create_order(connection: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    now = now_iso()
    access_token = create_order_access_token()
    cursor = connection.execute(
        """
        INSERT INTO orders (
            round_number, status, payment_status, created_at, updated_at,
            product_slug, product_name, product_short_name, product_tagline, product_price, product_image, product_category,
            size, quantity, total_amount, items_json, access_token,
            student_code, full_name, parent_phone,
            first_name, last_name, nickname, email, phone, school
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            get_current_phase(connection),
            "pending_payment",
            "awaiting_payment",
            now,
            now,
            payload["product"]["slug"],
            payload["product"]["name"],
            payload["product"]["shortName"],
            payload["product"]["tagline"],
            int(payload["product"]["price"]),
            payload["product"]["image"],
            payload["product"]["category"],
            payload["size"],
            payload["quantity"],
            payload["total_amount"],
            json.dumps(payload["items"], ensure_ascii=False),
            access_token,
            payload["customer"]["studentCode"],
            payload["customer"]["fullName"],
            payload["customer"]["parentPhone"],
            payload["customer"]["fullName"],
            "",
            "",
            payload["customer"]["email"],
            payload["customer"]["phone"],
            payload["customer"]["school"],
        ),
    )
    sequence_number = int(cursor.lastrowid)
    order_code = create_order_code(sequence_number, connection)
    connection.execute(
        "UPDATE orders SET order_code = ? WHERE internal_id = ?",
        (order_code, sequence_number),
    )
    items_for_check = payload.get("items") or []
    is_headband_only = bool(items_for_check) and all(
        (i.get("product") or {}).get("slug") == "fresh-headband" for i in items_for_check
    )
    if is_headband_only:
        khantok_ticket, khantok_ticket_claimed_at, khantok_ticket_already_claimed, khantok_ticket_value = False, None, False, None
    else:
        khantok_ticket, khantok_ticket_claimed_at, khantok_ticket_already_claimed, khantok_ticket_value = reserve_khantok_ticket(
            connection, sequence_number, payload["customer"]["studentCode"]
        )
    connection.execute(
        "UPDATE orders SET khantok_ticket = ?, khantok_ticket_claimed_at = ?, khantok_ticket_already_claimed = ?, khantok_ticket_value = ? WHERE internal_id = ?",
        (1 if khantok_ticket else 0, khantok_ticket_claimed_at, 1 if khantok_ticket_already_claimed else 0, khantok_ticket_value, sequence_number),
    )
    if is_headband_only:
        connection.execute(
            "UPDATE orders SET status = 'refund', payment_status = 'refund_pending', updated_at = ? WHERE internal_id = ?",
            (now, sequence_number),
        )
        # The audit row is best-effort: never fail the order because logging failed.
        with contextlib.suppress(sqlite3.Error):
            connection.execute(
                "INSERT INTO order_audit_log(order_code, event, detail, created_at) VALUES (?,?,?,?)",
                (order_code, "auto_refund_headband", "headband-only order auto-refunded on creation", now),
            )
    row = fetch_order_by_code(connection, order_code)
    assert row is not None
    return serialize_order(row, include_access_token=True)


def update_order(connection: sqlite3.Connection, order_code: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    existing_order = fetch_order_by_code(connection, order_code)
    if existing_order is None:
        return None

    delete_local_slip_file(existing_order["slip_stored_name"], existing_order["slip_storage_path"])
    now = now_iso()
    connection.execute(
        """
        UPDATE orders
        SET
            status = ?,
            payment_status = ?,
            updated_at = ?,
            product_slug = ?,
            product_name = ?,
            product_short_name = ?,
            product_tagline = ?,
            product_price = ?,
            product_image = ?,
            product_category = ?,
            size = ?,
            quantity = ?,
            total_amount = ?,
            items_json = ?,
            student_code = ?,
            full_name = ?,
            parent_phone = ?,
            first_name = ?,
            last_name = ?,
            nickname = ?,
            email = ?,
            phone = ?,
            school = ?,
            slip_original_name = NULL,
            slip_stored_name = NULL,
            slip_storage_path = NULL,
            slip_mime_type = NULL,
            slip_size = NULL,
            slip_uploaded_at = NULL
        WHERE order_code = ?
        """,
        (
            "pending_payment",
            "awaiting_payment",
            now,
            payload["product"]["slug"],
            payload["product"]["name"],
            payload["product"]["shortName"],
            payload["product"]["tagline"],
            int(payload["product"]["price"]),
            payload["product"]["image"],
            payload["product"]["category"],
            payload["size"],
            payload["quantity"],
            payload["total_amount"],
            json.dumps(payload["items"], ensure_ascii=False),
            payload["customer"]["studentCode"],
            payload["customer"]["fullName"],
            payload["customer"]["parentPhone"],
            payload["customer"]["fullName"],
            "",
            "",
            payload["customer"]["email"],
            payload["customer"]["phone"],
            payload["customer"]["school"],
            order_code,
        ),
    )
    row = fetch_order_by_code(connection, order_code)
    assert row is not None
    return serialize_order(row)


def update_order_slip(connection: sqlite3.Connection, order_code: str, slip: dict[str, Any]) -> dict[str, Any] | None:
    existing_order = fetch_order_by_code(connection, order_code)
    if existing_order is None:
        return None

    now = now_iso()
    connection.execute(
        """
        UPDATE orders
        SET
            status = ?,
            payment_status = ?,
            updated_at = ?,
            slip_original_name = ?,
            slip_stored_name = ?,
            slip_storage_path = ?,
            slip_mime_type = ?,
            slip_size = ?,
            slip_uploaded_at = ?
        WHERE order_code = ?
        """,
        (
            "waiting_confirm",
            "waiting_confirm",
            now,
            slip["originalName"],
            slip["storedName"],
            slip["storedPath"],
            slip["mimeType"],
            int(slip["size"]),
            slip["uploadedAt"],
            order_code,
        ),
    )
    row = fetch_order_by_code(connection, order_code)
    assert row is not None
    return serialize_order(row)
