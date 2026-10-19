"""Sales dashboard numbers for /admin (SU STORE v2)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .db import ORDER_STATUSES, TZ_BANGKOK

PAID_STATUSES = ("paid", "ready", "completed")
UNPAID_STATUSES = ("pending_payment", "waiting_confirm")


def dashboard(connection: sqlite3.Connection, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """Sales summary. created_at is written with a +07:00 offset, so its first 10 chars
    are already the Bangkok calendar date."""
    days = min(max(days, 7), 366)
    today = (now or datetime.now(TZ_BANGKOK)).astimezone(TZ_BANGKOK).date()
    paid = ",".join("?" * len(PAID_STATUSES))
    unpaid = ",".join("?" * len(UNPAID_STATUSES))

    counts = {r[0]: r[1] for r in connection.execute("SELECT status, COUNT(*) FROM store_orders GROUP BY status")}
    revenue = connection.execute(
        f"SELECT COALESCE(SUM(total_amount), 0) FROM store_orders WHERE status IN ({paid})", PAID_STATUSES
    ).fetchone()[0]
    awaiting = connection.execute(
        f"SELECT COALESCE(SUM(total_amount), 0) FROM store_orders WHERE status IN ({unpaid})", UNPAID_STATUSES
    ).fetchone()[0]

    first_day = today - timedelta(days=days - 1)
    per_day = {
        r[0]: (r[1], r[2])
        for r in connection.execute(
            f"SELECT substr(created_at, 1, 10) AS day, "
            f"COALESCE(SUM(CASE WHEN status IN ({paid}) THEN total_amount END), 0), "
            f"SUM(CASE WHEN status != 'cancelled' THEN 1 ELSE 0 END) "
            f"FROM store_orders WHERE substr(created_at, 1, 10) >= ? GROUP BY day",
            (*PAID_STATUSES, first_day.isoformat()),
        )
    }
    by_day = []
    for offset in range(days):
        day = (first_day + timedelta(days=offset)).isoformat()
        amount, orders = per_day.get(day, (0, 0))
        by_day.append({"date": day, "paidAmount": amount, "orders": orders})

    by_variant = [
        {
            "productName": r["product_name"],
            "variantLabel": r["variant_label"],
            "paidQuantity": r["paid_qty"],
            "unpaidQuantity": r["unpaid_qty"],
            "paidAmount": r["paid_amount"],
        }
        for r in connection.execute(
            f"SELECT i.product_name, i.variant_label, "
            f"SUM(CASE WHEN o.status IN ({paid}) THEN i.quantity ELSE 0 END) AS paid_qty, "
            f"SUM(CASE WHEN o.status IN ({unpaid}) THEN i.quantity ELSE 0 END) AS unpaid_qty, "
            f"SUM(CASE WHEN o.status IN ({paid}) THEN i.quantity * i.unit_price ELSE 0 END) AS paid_amount "
            f"FROM store_order_items i JOIN store_orders o ON o.id = i.order_id "
            f"WHERE o.status != 'cancelled' "
            f"GROUP BY i.product_id, i.product_name, i.variant_id, i.variant_label "
            f"ORDER BY i.product_name, MIN(i.variant_id)",
            (*PAID_STATUSES, *UNPAID_STATUSES, *PAID_STATUSES),
        )
    ]
    return {
        "paidAmount": revenue,
        "awaitingAmount": awaiting,
        "orderCount": sum(v for k, v in counts.items() if k != "cancelled"),
        "statusCounts": {s: counts.get(s, 0) for s in ORDER_STATUSES},
        "byDay": by_day,
        "byVariant": by_variant,
    }
