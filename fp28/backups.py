"""In-database order backups and the scheduled backup thread."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .database import open_db
from .orders import list_orders
from .timeutil import TZ_BANGKOK, now_iso


def create_backup(connection: sqlite3.Connection) -> dict[str, Any]:
    orders, _ = list_orders(connection, page=1, per_page=999999)
    now = now_iso()
    orders_json = json.dumps(orders, ensure_ascii=False)
    cursor = connection.execute(
        "INSERT INTO order_backups (created_at, order_count, orders_json) VALUES (?, ?, ?)",
        (now, len(orders), orders_json),
    )
    connection.commit()
    return {"id": cursor.lastrowid, "createdAt": now, "label": "", "orderCount": len(orders)}


def list_backups(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, created_at, label, order_count FROM order_backups ORDER BY id DESC"
    ).fetchall()
    return [
        {"id": r["id"], "createdAt": r["created_at"], "label": r["label"], "orderCount": r["order_count"]}
        for r in rows
    ]


def get_backup(connection: sqlite3.Connection, backup_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT id, created_at, label, order_count, orders_json FROM order_backups WHERE id = ?",
        (backup_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "createdAt": row["created_at"],
        "label": row["label"],
        "orderCount": row["order_count"],
        "orders": json.loads(row["orders_json"]),
    }


def delete_backup(connection: sqlite3.Connection, backup_id: int) -> bool:
    result = connection.execute("DELETE FROM order_backups WHERE id = ?", (backup_id,))
    connection.commit()
    return result.rowcount > 0


def run_scheduled_backup() -> None:
    """Daemon thread: create one automatic backup per day at midnight Bangkok time, keep last 30."""
    import time
    last_backup_date: str | None = None
    while True:
        try:
            now_bkk = datetime.now(TZ_BANGKOK)
            today = now_bkk.strftime("%Y-%m-%d")
            # trigger between 00:00 and 00:05
            if now_bkk.hour == 0 and now_bkk.minute < 5 and last_backup_date != today:
                with open_db() as conn:
                    # prune: keep most recent 30 auto-backups
                    rows = conn.execute(
                        "SELECT id FROM order_backups WHERE label LIKE 'auto-%' ORDER BY id DESC LIMIT -1 OFFSET 30"
                    ).fetchall()
                    for row in rows:
                        conn.execute("DELETE FROM order_backups WHERE id = ?", (row["id"],))
                    backup = create_backup(conn)
                    conn.execute(
                        "UPDATE order_backups SET label = ? WHERE id = ?",
                        (f"auto-{today}", backup["id"]),
                    )
                    conn.commit()
                last_backup_date = today
                print(f"[backup] auto backup created for {today} (id={backup['id']}, orders={backup['orderCount']})")
        except Exception as exc:
            import sys as _sys
            print(f"[backup] scheduled backup failed: {exc}", file=_sys.stderr)
        time.sleep(60)
