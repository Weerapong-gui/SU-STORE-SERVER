"""Summaries, live stats, analytics and the CSV export for the FP28 admin."""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
from typing import Any

from . import config
from .database import open_db


def create_orders_summary(connection: sqlite3.Connection, round_filter: int = 0) -> dict[str, int]:
    settings_rows = connection.execute("SELECT key, value FROM site_settings WHERE key IN ('khantok_quota_100','khantok_quota_50')").fetchall()
    settings_map = {row["key"]: row["value"] for row in settings_rows}
    quota_100 = int(settings_map.get("khantok_quota_100") or config.KHANTOK_QUOTA_100)
    quota_50 = int(settings_map.get("khantok_quota_50") or config.KHANTOK_QUOTA_50)
    round_where = "AND round_number = ?" if round_filter > 0 else ""
    round_params: list[Any] = [round_filter] if round_filter > 0 else []
    used_100 = int(connection.execute(
        "SELECT COUNT(*) AS c FROM khantok_ticket_claims WHERE ticket_value = 100"
    ).fetchone()["c"])
    used_50 = int(connection.execute(
        "SELECT COUNT(*) AS c FROM khantok_ticket_claims WHERE ticket_value = 50"
    ).fetchone()["c"])
    counts = {
        row["status"]: row["cnt"]
        for row in connection.execute(
            f"SELECT status, COUNT(*) AS cnt FROM orders WHERE 1=1 {round_where} GROUP BY status",
            round_params,
        ).fetchall()
    }
    total_row = connection.execute(
        f"SELECT COUNT(*) AS cnt FROM orders WHERE status NOT IN ('refund','refunded') {round_where}",
        round_params,
    ).fetchone()
    khantok_station_row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS claimed,
            SUM(CASE WHEN (received_at IS NULL OR received_at = '') AND status != 'received' THEN 1 ELSE 0 END) AS not_received
        FROM orders
        WHERE khantok_station_claimed_at IS NOT NULL
          AND khantok_station_claimed_at != ''
          AND status NOT IN ('rejected','cancelled','refund','refunded')
          {round_where}
        """,
        round_params,
    ).fetchone()
    # Count per-category qty from items_json so multi-item orders are fully counted
    # Exclude rejected, cancelled, refund, and refunded orders from product quantity totals
    category_counts: dict[str, int] = {}
    for row in connection.execute(
        f"SELECT items_json, product_category, quantity FROM orders WHERE status NOT IN ('rejected','cancelled','refund','refunded') {round_where}",
        round_params,
    ).fetchall():
        counted = False
        if row["items_json"]:
            try:
                parsed = json.loads(row["items_json"])
                if isinstance(parsed, list) and parsed:
                    for item in parsed:
                        cat = (item.get("product") or {}).get("category") or ""
                        cat = {"shirt": "single"}.get(cat, cat)
                        qty = item.get("quantity") or 0
                        if cat:
                            category_counts[cat] = category_counts.get(cat, 0) + int(qty)
                    counted = True
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if not counted:
            cat = row["product_category"] or ""
            if cat:
                category_counts[cat] = category_counts.get(cat, 0) + int(row["quantity"] or 0)
    return {
        "total": int(total_row["cnt"] if total_row else 0),
        "pendingPayment": counts.get("pending_payment", 0),
        "waitingConfirm": counts.get("waiting_confirm", 0),
        "paid": counts.get("paid", 0) + counts.get("preparing", 0) + counts.get("shipped", 0) + counts.get("received", 0),
        "received": counts.get("received", 0),
        "rejected": counts.get("rejected", 0),
        "cancelled": counts.get("cancelled", 0),
        "khantokTicket100Quota": quota_100,
        "khantokTicket100Used": used_100,
        "khantokTicket100Remaining": max(quota_100 - used_100, 0),
        "khantokTicket50Quota": quota_50,
        "khantokTicket50Used": used_50,
        "khantokTicket50Remaining": max(quota_50 - used_50, 0),
        "khantokStationClaimed": int(khantok_station_row["claimed"] or 0),
        "khantokStationClaimedNotReceived": int(khantok_station_row["not_received"] or 0),
        "qtySingle": category_counts.get("single", 0),
        "qtyJacket": category_counts.get("jacket", 0),
        "qtyHeadband": category_counts.get("headband", 0),
    }


_SUMMARY_CACHE: dict[int, tuple[float, dict[str, int]]] = {}


_SUMMARY_CACHE_LOCK = threading.Lock()


_SUMMARY_CACHE_TTL = float(os.environ.get("ORDER_API_SUMMARY_CACHE_TTL", "2.0"))


def create_orders_summary_cached(
    connection: sqlite3.Connection, round_filter: int = 0
) -> dict[str, int]:
    """create_orders_summary() ที่แชร์ผลข้าม request ไม่กี่วินาที

    summary สแกน orders ทั้งตารางแล้ว json.loads ทุกแถว จึงแพงเกินกว่าจะให้ทุก
    request/stream คำนวณซ้ำเอง ค่าที่ได้เป็นตัวนับ ไม่ใช่ข้อมูลออเดอร์ ช้าไม่กี่
    วินาทีจึงยอมรับได้
    """
    if _SUMMARY_CACHE_TTL <= 0:
        return create_orders_summary(connection, round_filter)
    with _SUMMARY_CACHE_LOCK:
        entry = _SUMMARY_CACHE.get(round_filter)
        if entry is not None and (time.monotonic() - entry[0]) <= _SUMMARY_CACHE_TTL:
            return dict(entry[1])
    summary = create_orders_summary(connection, round_filter)
    with _SUMMARY_CACHE_LOCK:
        if len(_SUMMARY_CACHE) > 32:
            _SUMMARY_CACHE.clear()
        _SUMMARY_CACHE[round_filter] = (time.monotonic(), summary)
    return dict(summary)


_STATS_SNAPSHOT: tuple[float, dict[str, Any]] | None = None


_STATS_SNAPSHOT_LOCK = threading.Lock()


_STATS_SNAPSHOT_TTL = float(os.environ.get("ORDER_API_STATS_SNAPSHOT_TTL", "5.0"))


def _compute_stats_snapshot() -> dict[str, Any]:
    with open_db() as connection:
        summary = create_orders_summary_cached(connection)
        rev_row = connection.execute(
            "SELECT COALESCE(SUM(total_amount),0) AS r FROM orders WHERE status IN ('paid','preparing','shipped','received')"
        ).fetchone()
        revenue = int(rev_row["r"] if rev_row else 0)
        school_cnt = connection.execute(
            "SELECT COUNT(DISTINCT school) AS c FROM orders"
        ).fetchone()["c"]
        cost_rows = connection.execute(
            "SELECT items_json, product_category, quantity FROM orders WHERE status IN ('paid','preparing','shipped','received')"
        ).fetchall()
    cq: dict[str, int] = {}
    for row in cost_rows:
        ok = False
        if row["items_json"]:
            try:
                for itm in json.loads(row["items_json"]):
                    cat = (itm.get("product") or {}).get("category") or ""
                    if cat:
                        cq[cat] = cq.get(cat, 0) + int(itm.get("quantity") or 0)
                ok = True
            except Exception:
                pass
        if not ok:
            cat = row["product_category"] or ""
            if cat:
                cq[cat] = cq.get(cat, 0) + int(row["quantity"] or 0)
    profit = revenue - sum(cq.get(c, 0) * p for c, p in PRODUCT_COST.items())
    return {
        "summary": summary,
        "analytics": {
            "total": summary["total"],
            "revenue": revenue,
            "schoolCount": school_cnt,
            "profit": profit,
        },
    }


def get_stats_snapshot() -> dict[str, Any]:
    """ส่วนที่มาจาก DB ของ payload /admin/stats-stream คำนวณรอบเดียวแล้วแชร์ทุก connection

    ก่อนหน้านี้ทุก connection ที่ต่ออยู่ query ชุดเดียวกันเองทุก tick — 16 แท็บ
    เปิดค้าง = ทำงานเดียวกัน 16 รอบ กิน CPU จน request อื่นโดน GIL starve
    """
    global _STATS_SNAPSHOT
    snapshot = _STATS_SNAPSHOT
    if snapshot is not None and (time.monotonic() - snapshot[0]) <= _STATS_SNAPSHOT_TTL:
        return snapshot[1]
    with _STATS_SNAPSHOT_LOCK:
        snapshot = _STATS_SNAPSHOT
        if snapshot is not None and (time.monotonic() - snapshot[0]) <= _STATS_SNAPSHOT_TTL:
            return snapshot[1]
        payload = _compute_stats_snapshot()
        _STATS_SNAPSHOT = (time.monotonic(), payload)
        return payload


_STATS_STREAM_MAX = int(os.environ.get("ORDER_API_STATS_STREAM_MAX", "8"))


_STATS_STREAM_MAX_SECONDS = float(
    os.environ.get("ORDER_API_STATS_STREAM_MAX_SECONDS", "1800")
)


_stats_stream_count = 0


_stats_stream_lock = threading.Lock()


def stats_stream_acquire() -> bool:
    global _stats_stream_count
    with _stats_stream_lock:
        if _stats_stream_count >= _STATS_STREAM_MAX:
            return False
        _stats_stream_count += 1
        return True


def stats_stream_release() -> None:
    global _stats_stream_count
    with _stats_stream_lock:
        _stats_stream_count = max(0, _stats_stream_count - 1)


def export_orders_csv(orders: list[dict[str, Any]], ocr_map: dict[str, dict] | None = None) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "order_id", "status", "payment_status", "order_total_amount",
        "item_product_name", "item_category", "item_size", "item_quantity", "item_unit_price", "item_total_amount",
        "created_at", "full_name", "student_code", "phone", "email", "school", "parent_phone",
        "khantok_ticket", "ocr_status", "ocr_ref",
    ])
    for order in orders:
        c = order.get("customer") or {}
        ocr = (ocr_map or {}).get(order.get("id", ""), {})
        common = [
            order.get("id", ""),
            order.get("status", ""),
            order.get("paymentStatus", ""),
            order.get("totalAmount", ""),
        ]
        common_tail = [
            order.get("createdAt", ""),
            c.get("fullName", ""),
            c.get("studentCode", ""),
            c.get("phone", ""),
            c.get("email", ""),
            c.get("school", ""),
            c.get("parentPhone", ""),
            "yes" if order.get("khantokTicket") else "no",
            ocr.get("status", ""),
            ocr.get("ref_number", ""),
        ]
        items = order.get("items") or []
        if not items:
            p = order.get("product") or {}
            writer.writerow([
                *common,
                p.get("name", ""), p.get("category", ""),
                order.get("size", ""), order.get("quantity", ""),
                "", order.get("totalAmount", ""),
                *common_tail,
            ])
        else:
            for item in items:
                ip = item.get("product") or {}
                writer.writerow([
                    *common,
                    ip.get("name", ""), ip.get("category", ""),
                    item.get("size", ""), item.get("quantity", ""),
                    item.get("unitPrice", ""), item.get("totalAmount", ""),
                    *common_tail,
                ])
    return output.getvalue()


def get_product_breakdown(connection: sqlite3.Connection, category: str, round_filter: int = 0) -> dict[str, Any]:
    SIZE_ORDER = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "6XL", "7XL"]
    COLOR_ORDER = ["Blue", "Red", "White"]
    COLOR_THAI = {"Blue": "สีน้ำเงิน", "Red": "สีแดง", "White": "สีขาว"}
    # map legacy product_category values (shirt/jacket/headband) to slugs (single/jacket/headband)
    CATEGORY_TO_SLUG = {"shirt": "single", "jacket": "jacket", "headband": "headband"}
    rw = "WHERE status NOT IN ('rejected', 'cancelled')" + (" AND round_number = ?" if round_filter > 0 else "")
    rp: list[Any] = [round_filter] if round_filter > 0 else []

    if category == "jacket":
        # color → size → count
        color_size: dict[str, dict[str, int]] = {}
        for row in connection.execute(f"SELECT items_json, product_category, size, quantity FROM orders {rw}", rp).fetchall():
            items_processed = False
            if row["items_json"]:
                try:
                    parsed = json.loads(row["items_json"])
                    if isinstance(parsed, list):
                        for item in parsed:
                            raw_cat = (item.get("product") or {}).get("category") or ""
                            if CATEGORY_TO_SLUG.get(raw_cat, raw_cat) != "jacket":
                                continue
                            qty = int(item.get("quantity") or 0)
                            raw_size = (item.get("size") or "").strip()
                            parts = raw_size.split(" / ")
                            sz = parts[0].strip() or "ONE SIZE"
                            color = parts[1].strip() if len(parts) > 1 else "(ไม่ระบุสี)"
                            color_size.setdefault(color, {})
                            color_size[color][sz] = color_size[color].get(sz, 0) + qty
                        items_processed = True
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            if not items_processed and CATEGORY_TO_SLUG.get(row["product_category"] or "", row["product_category"] or "") == "jacket":
                qty = int(row["quantity"] or 0)
                raw_size = (row["size"] or "").strip()
                parts = raw_size.split(" / ")
                sz = parts[0].strip() or "ONE SIZE"
                color = parts[1].strip() if len(parts) > 1 else "(ไม่ระบุสี)"
                color_size.setdefault(color, {})
                color_size[color][sz] = color_size[color].get(sz, 0) + qty

        groups = []
        for color in COLOR_ORDER + [c for c in color_size if c not in COLOR_ORDER]:
            if color not in color_size:
                continue
            sz_map = color_size[color]
            sorted_sizes = sorted(sz_map.keys(), key=lambda k: SIZE_ORDER.index(k) if k in SIZE_ORDER else 99)
            rows = [{"label": sz, "count": sz_map[sz]} for sz in sorted_sizes]
            groups.append({
                "color": color,
                "colorThai": COLOR_THAI.get(color, color),
                "total": sum(sz_map.values()),
                "rows": rows,
            })
        return {"category": category, "groups": groups}

    # single / headband — flat rows
    counts: dict[str, int] = {}
    for row in connection.execute(f"SELECT items_json, product_category, size, quantity FROM orders {rw}", rp).fetchall():
        items_processed = False
        if row["items_json"]:
            try:
                parsed = json.loads(row["items_json"])
                if isinstance(parsed, list):
                    for item in parsed:
                        raw_cat = (item.get("product") or {}).get("category") or ""
                        item_slug = CATEGORY_TO_SLUG.get(raw_cat, raw_cat)
                        if item_slug != category:
                            continue
                        qty = int(item.get("quantity") or 0)
                        if category == "headband":
                            key = (item.get("school") or "").strip() or "(ไม่ระบุสำนัก)"
                        else:
                            key = (item.get("size") or "").strip() or "ONE SIZE"
                        counts[key] = counts.get(key, 0) + qty
                    items_processed = True
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        pc = row["product_category"] or ""
        pc_slug = CATEGORY_TO_SLUG.get(pc, pc)
        if not items_processed and (row["product_slug"] == category or pc_slug == category):
            qty = int(row["quantity"] or 0)
            key = "(ไม่ระบุสำนัก)" if category == "headband" else (row["size"] or "").strip() or "ONE SIZE"
            counts[key] = counts.get(key, 0) + qty

    if category == "single":
        sorted_keys = sorted(counts.keys(), key=lambda k: SIZE_ORDER.index(k) if k in SIZE_ORDER else 99)
    else:
        sorted_keys = sorted(counts.keys(), key=lambda k: -counts[k])

    return {"category": category, "rows": [{"label": k, "count": counts[k]} for k in sorted_keys]}


PRODUCT_COST: dict[str, int] = {"single": 158, "jacket": 685, "headband": 20}


def get_analytics(connection: sqlite3.Connection) -> dict[str, Any]:
    exclude_refund = "status NOT IN ('refund','refunded')"
    total_row = connection.execute(f"SELECT COUNT(*) AS cnt FROM orders WHERE {exclude_refund}").fetchone()
    total = int(total_row["cnt"] if total_row else 0)
    school_count_row = connection.execute(f"SELECT COUNT(DISTINCT school) AS cnt FROM orders WHERE {exclude_refund}").fetchone()
    school_count = int(school_count_row["cnt"] if school_count_row else 0)
    student_count_row = connection.execute(
        f"SELECT COUNT(DISTINCT student_code) AS cnt FROM orders WHERE student_code IS NOT NULL AND student_code != '' AND {exclude_refund}"
    ).fetchone()
    student_count = int(student_count_row["cnt"] if student_count_row else 0)
    by_school = connection.execute(
        f"SELECT school, COUNT(*) AS cnt FROM orders WHERE {exclude_refund} GROUP BY school ORDER BY cnt DESC"
    ).fetchall()
    by_product = connection.execute(
        f"SELECT product_name, COUNT(*) AS cnt FROM orders WHERE {exclude_refund} GROUP BY product_name ORDER BY cnt DESC"
    ).fetchall()
    by_status = connection.execute(
        "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status ORDER BY cnt DESC"
    ).fetchall()
    by_size = connection.execute(
        f"SELECT size, COUNT(*) AS cnt FROM orders WHERE size != '' AND {exclude_refund} GROUP BY size ORDER BY cnt DESC LIMIT 20"
    ).fetchall()
    by_day = connection.execute(
        f"SELECT DATE(created_at, '+7 hours') AS day, COUNT(*) AS cnt FROM orders WHERE DATE(created_at, '+7 hours') != '2026-05-17' AND {exclude_refund} GROUP BY day ORDER BY day"
    ).fetchall()
    # Revenue + cost: exclude headband items (refund) and exclude refund/refunded orders
    confirmed_rows = connection.execute(
        "SELECT items_json, product_category, product_price, quantity, total_amount FROM orders WHERE status IN ('paid','preparing','shipped','received')"
    ).fetchall()
    revenue = 0
    cost_qty: dict[str, int] = {}
    for row in confirmed_rows:
        counted = False
        if row["items_json"]:
            try:
                parsed = json.loads(row["items_json"])
                if isinstance(parsed, list) and parsed:
                    for item in parsed:
                        cat = (item.get("product") or {}).get("category") or ""
                        qty = int(item.get("quantity") or 0)
                        up = int(item.get("unitPrice") or (item.get("product") or {}).get("price") or 0)
                        if cat == "headband":
                            continue
                        revenue += up * qty
                        if cat:
                            cost_qty[cat] = cost_qty.get(cat, 0) + qty
                    counted = True
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if not counted:
            cat = row["product_category"] or ""
            if cat != "headband":
                revenue += int(row["total_amount"] or 0)
                if cat:
                    cost_qty[cat] = cost_qty.get(cat, 0) + int(row["quantity"] or 0)
    refunded_row = connection.execute(
        "SELECT COALESCE(SUM(total_amount), 0) AS r, COUNT(*) AS c FROM orders WHERE status IN ('refund','refunded')"
    ).fetchone()
    refunded_amount = int(refunded_row["r"] if refunded_row else 0)
    refunded_count = int(refunded_row["c"] if refunded_row else 0)
    total_cost = sum(cost_qty.get(cat, 0) * price for cat, price in PRODUCT_COST.items())
    profit = revenue - total_cost
    return {
        "total": total,
        "revenue": revenue,
        "schoolCount": school_count,
        "studentCount": student_count,
        "profit": profit,
        "refundedAmount": refunded_amount,
        "refundedCount": refunded_count,
        "bySchool": [[row["school"], row["cnt"]] for row in by_school],
        "byProduct": [[row["product_name"], row["cnt"]] for row in by_product],
        "byStatus": [[row["status"], row["cnt"]] for row in by_status],
        "bySize": [[row["size"], row["cnt"]] for row in by_size],
        "byDay": [[row["day"], row["cnt"]] for row in by_day],
    }
