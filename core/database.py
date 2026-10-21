"""SQLite schema, the connection pool, a short-lived read cache and the audit log."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading

from store import db as store_db

from . import config
from .auth import compute_claim_token, compute_super_token
from .timeutil import now_iso


def log_audit(connection: sqlite3.Connection, order_code: str, event: str, detail: str = "") -> None:
    connection.execute(
        "INSERT INTO order_audit_log (order_code, event, detail, created_at) VALUES (?, ?, ?, ?)",
        (order_code, event, detail, now_iso()),
    )


def ensure_db() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.SLIPS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.DB_PATH, timeout=30) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS site_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_order_code ON order_audit_log(order_code);
            CREATE INDEX IF NOT EXISTS idx_audit_created_at ON order_audit_log(created_at);
            CREATE TABLE IF NOT EXISTS slip_ocr_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                ref_number TEXT,
                amount REAL,
                date TEXT,
                sender TEXT,
                bank TEXT,
                duplicate_of TEXT,
                raw_reason TEXT,
                checked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ocr_ref ON slip_ocr_results(ref_number);
            CREATE TABLE IF NOT EXISTS admin_users (
                username TEXT PRIMARY KEY,
                claim_token TEXT NOT NULL,
                super_token TEXT,
                is_superadmin INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT 'system',
                created_at TEXT NOT NULL
            );
            """
        )
        if config.PICKUP_PASSWORD and not connection.execute(
            "SELECT 1 FROM admin_users WHERE username = ?", (config.PICKUP_USERNAME,)
        ).fetchone():
            seed_now = now_iso()
            connection.execute(
                """
                INSERT INTO admin_users
                    (username, claim_token, super_token, is_superadmin, created_by, created_at)
                VALUES (?, ?, ?, 1, 'system', ?)
                """,
                (
                    config.PICKUP_USERNAME,
                    compute_claim_token(config.PICKUP_USERNAME, config.PICKUP_PASSWORD),
                    compute_super_token(config.PICKUP_USERNAME, config.PICKUP_PASSWORD),
                    seed_now,
                ),
            )
        store_db.ensure_store_db(connection)


_CONN_POOL: list[sqlite3.Connection] = []


_CONN_POOL_LOCK = threading.Lock()


_CONN_POOL_MAX = int(os.environ.get("ORDER_API_CONN_POOL_MAX", "32"))


def _new_db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


class _PooledConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._broken = False

    def __enter__(self) -> sqlite3.Connection:
        return self._connection

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        except Exception:
            self._broken = True
        with _CONN_POOL_LOCK:
            if self._broken or len(_CONN_POOL) >= _CONN_POOL_MAX:
                with contextlib.suppress(Exception):
                    self._connection.close()
            else:
                _CONN_POOL.append(self._connection)
        return False


def open_db() -> _PooledConnection:
    with _CONN_POOL_LOCK:
        connection = _CONN_POOL.pop() if _CONN_POOL else None
    if connection is None:
        connection = _new_db_connection()
    return _PooledConnection(connection)


