"""SQLite schema, the connection pool, a short-lived read cache and the audit log."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from typing import Any

from store import db as store_db

from . import config
from .auth import create_order_access_token
from .config import CLAIM_STATION_TOKEN, DEFAULT_PRODUCTS, PICKUP_PASSWORD, PICKUP_USERNAME, SUPER_TOKEN
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
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS orders (
              internal_id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_code TEXT UNIQUE,
              round_number INTEGER NOT NULL,
              status TEXT NOT NULL,
              payment_status TEXT NOT NULL,
              khantok_ticket INTEGER NOT NULL DEFAULT 0,
              khantok_ticket_claimed_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              product_slug TEXT NOT NULL,
              product_name TEXT NOT NULL,
              product_short_name TEXT NOT NULL,
              product_tagline TEXT NOT NULL,
              product_price INTEGER NOT NULL,
              product_image TEXT NOT NULL,
              product_category TEXT NOT NULL,
              size TEXT NOT NULL,
              quantity INTEGER NOT NULL,
              total_amount INTEGER NOT NULL,
              items_json TEXT NOT NULL DEFAULT '[]',
              student_code TEXT NOT NULL DEFAULT '',
              full_name TEXT NOT NULL DEFAULT '',
              parent_phone TEXT NOT NULL DEFAULT '',
              first_name TEXT NOT NULL,
              last_name TEXT NOT NULL,
              nickname TEXT NOT NULL,
              email TEXT NOT NULL,
              phone TEXT NOT NULL,
              school TEXT NOT NULL,
              access_token TEXT NOT NULL,
              slip_original_name TEXT,
              slip_stored_name TEXT,
              slip_storage_path TEXT,
              slip_mime_type TEXT,
              slip_size INTEGER,
              slip_uploaded_at TEXT
          )
          """
      )
      columns = {
          row[1]
          for row in connection.execute("PRAGMA table_info(orders)").fetchall()
      }
      if "access_token" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN access_token TEXT")
      if "slip_storage_path" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN slip_storage_path TEXT")
      if "khantok_ticket" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN khantok_ticket INTEGER NOT NULL DEFAULT 0")
      if "khantok_ticket_claimed_at" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN khantok_ticket_claimed_at TEXT")
      if "lucky_ticket" in columns:
          connection.execute(
              """
              UPDATE orders
              SET khantok_ticket = lucky_ticket
              WHERE khantok_ticket = 0 AND lucky_ticket = 1
              """
          )
      if "lucky_ticket_claimed_at" in columns:
          connection.execute(
              """
              UPDATE orders
              SET khantok_ticket_claimed_at = lucky_ticket_claimed_at
              WHERE
                  (khantok_ticket_claimed_at IS NULL OR khantok_ticket_claimed_at = '')
                  AND lucky_ticket_claimed_at IS NOT NULL
                  AND lucky_ticket_claimed_at != ''
              """
          )
      if "khantok_ticket_already_claimed" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN khantok_ticket_already_claimed INTEGER NOT NULL DEFAULT 0")
      if "khantok_ticket_value" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN khantok_ticket_value INTEGER")
      if "khantok_station_claimed_at" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN khantok_station_claimed_at TEXT")
      if "student_code" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN student_code TEXT NOT NULL DEFAULT ''")
      if "full_name" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
      if "parent_phone" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN parent_phone TEXT NOT NULL DEFAULT ''")
      if "items_json" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN items_json TEXT NOT NULL DEFAULT '[]'")
      if "admin_note" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN admin_note TEXT")
      if "received_at" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN received_at TEXT")
      if "received_by" not in columns:
          connection.execute("ALTER TABLE orders ADD COLUMN received_by TEXT")
      existing_rows = connection.execute(
          "SELECT internal_id FROM orders WHERE access_token IS NULL OR access_token = ''"
      ).fetchall()
      for row in existing_rows:
          connection.execute(
              "UPDATE orders SET access_token = ? WHERE internal_id = ?",
              (create_order_access_token(), row[0]),
          )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS khantok_ticket_claims (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id INTEGER NOT NULL UNIQUE,
              claimed_at TEXT NOT NULL,
              ticket_value INTEGER NOT NULL DEFAULT 100,
              FOREIGN KEY (order_id) REFERENCES orders(internal_id)
          )
          """
      )
      existing_tables = {
          row[0]
          for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
      }
      if "lucky_ticket_claims" in existing_tables:
          connection.execute(
              """
              INSERT OR IGNORE INTO khantok_ticket_claims (order_id, claimed_at)
              SELECT order_id, claimed_at FROM lucky_ticket_claims
              """
          )
      claim_columns = {
          row[1]
          for row in connection.execute("PRAGMA table_info(khantok_ticket_claims)").fetchall()
      }
      if "ticket_value" not in claim_columns:
          connection.execute("ALTER TABLE khantok_ticket_claims ADD COLUMN ticket_value INTEGER NOT NULL DEFAULT 100")
      connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_order_code ON orders(order_code)")
      # Hot-path indexes: the khantok de-dup check, list/filter, analytics group-bys and
      # check-order lookups all filter/sort on these columns. Additive & idempotent.
      connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_student_code ON orders(student_code)")
      connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
      connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_round_number ON orders(round_number)")
      connection.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS products (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slug TEXT UNIQUE NOT NULL,
              name TEXT NOT NULL,
              short_name TEXT NOT NULL,
              tagline TEXT NOT NULL,
              description TEXT NOT NULL,
              price INTEGER NOT NULL,
              category TEXT NOT NULL,
              requires_size INTEGER NOT NULL DEFAULT 1,
              requires_school INTEGER NOT NULL DEFAULT 1,
              available INTEGER NOT NULL DEFAULT 1,
              image_path TEXT NOT NULL DEFAULT '',
              sort_order INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS site_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS order_audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_code TEXT NOT NULL,
              event TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
          )
          """
      )
      connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_order_code ON order_audit_log(order_code)")
      connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_created_at ON order_audit_log(created_at)")
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS order_feedback (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_code TEXT NOT NULL UNIQUE,
              rating INTEGER NOT NULL,
              comment TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
          )
          """
      )
      connection.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON order_feedback(created_at)")
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS display2_picks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_internal_id INTEGER NOT NULL,
              order_code TEXT NOT NULL,
              item_index INTEGER NOT NULL,
              station TEXT NOT NULL,
              product_slug TEXT NOT NULL,
              product_name TEXT NOT NULL,
              size TEXT NOT NULL,
              quantity INTEGER NOT NULL,
              student_code TEXT NOT NULL,
              nickname TEXT NOT NULL,
              full_name TEXT NOT NULL,
              queued_at TEXT NOT NULL,
              queued_by TEXT NOT NULL,
              picked_at TEXT,
              picked_by TEXT,
              undone_at TEXT,
              FOREIGN KEY (order_internal_id) REFERENCES orders(internal_id)
          )
          """
      )
      connection.execute(
          "CREATE INDEX IF NOT EXISTS idx_display2_picks_station_active "
          "ON display2_picks(station, picked_at)"
      )
      connection.execute(
          "CREATE INDEX IF NOT EXISTS idx_display2_picks_order "
          "ON display2_picks(order_internal_id)"
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS display2_assets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              station TEXT NOT NULL,
              category TEXT,
              filename TEXT NOT NULL UNIQUE,
              uploaded_at TEXT NOT NULL,
              uploaded_by TEXT NOT NULL DEFAULT ''
          )
          """
      )
      connection.execute(
          "CREATE INDEX IF NOT EXISTS idx_display2_assets_station "
          "ON display2_assets(station, category)"
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS display1_state (
              username     TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              updated_at   TEXT NOT NULL
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS display1_state (
              username     TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              updated_at   TEXT NOT NULL
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS slip_ocr_results (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              order_code   TEXT UNIQUE NOT NULL,
              status       TEXT NOT NULL DEFAULT 'pending',
              ref_number   TEXT,
              amount       REAL,
              date         TEXT,
              sender       TEXT,
              bank         TEXT,
              duplicate_of TEXT,
              raw_reason   TEXT,
              checked_at   TEXT
          )
          """
      )
      connection.execute("CREATE INDEX IF NOT EXISTS idx_ocr_ref ON slip_ocr_results(ref_number)")
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS order_extra_slips (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              order_code   TEXT NOT NULL,
              original_name TEXT,
              stored_name  TEXT NOT NULL,
              storage_path TEXT,
              mime_type    TEXT,
              file_size    INTEGER,
              uploaded_at  TEXT NOT NULL
          )
          """
      )
      connection.execute("CREATE INDEX IF NOT EXISTS idx_extra_slips_order_code ON order_extra_slips(order_code)")
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS order_backups (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              label TEXT NOT NULL DEFAULT '',
              order_count INTEGER NOT NULL DEFAULT 0,
              orders_json TEXT NOT NULL DEFAULT '[]'
          )
          """
      )
      connection.execute(
          """
          CREATE TABLE IF NOT EXISTS admin_users (
              username TEXT PRIMARY KEY,
              claim_token TEXT NOT NULL,
              super_token TEXT,
              is_superadmin INTEGER NOT NULL DEFAULT 0,
              created_by TEXT NOT NULL DEFAULT 'system',
              created_at TEXT NOT NULL
          )
          """
      )
      if PICKUP_PASSWORD and not connection.execute(
          "SELECT 1 FROM admin_users WHERE username=?", (PICKUP_USERNAME,)
      ).fetchone():
          seed_now = now_iso()
          connection.execute(
              "INSERT OR IGNORE INTO admin_users (username,claim_token,super_token,is_superadmin,created_by,created_at) VALUES (?,?,?,1,'system',?)",
              (PICKUP_USERNAME, CLAIM_STATION_TOKEN, SUPER_TOKEN, seed_now),
          )
      product_count_row = connection.execute("SELECT COUNT(*) FROM products").fetchone()
      if product_count_row[0] == 0:
          seed_now = now_iso()
          for p in DEFAULT_PRODUCTS:
              connection.execute(
                  """
                  INSERT OR IGNORE INTO products
                  (slug, name, short_name, tagline, description, price, category,
                   requires_size, requires_school, available, image_path, sort_order, created_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                  """,
                  (
                      p["slug"], p["name"], p["short_name"], p["tagline"], p["description"],
                      p["price"], p["category"], p["requires_size"], p["requires_school"],
                      p["image_path"], p["sort_order"], seed_now, seed_now,
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


_READ_CACHE: dict[str, tuple[float, Any]] = {}


_READ_CACHE_LOCK = threading.Lock()


_READ_CACHE_TTL = float(os.environ.get("ORDER_API_READ_CACHE_TTL", "1.0"))


_READ_CACHE_MAX = int(os.environ.get("ORDER_API_READ_CACHE_MAX", "5000"))


def cache_get(key: str) -> Any:
    if _READ_CACHE_TTL <= 0:
        return None
    with _READ_CACHE_LOCK:
        entry = _READ_CACHE.get(key)
    if entry is None:
        return None
    ts, value = entry
    if (time.monotonic() - ts) > _READ_CACHE_TTL:
        return None
    return value


def cache_set(key: str, value: Any) -> None:
    if _READ_CACHE_TTL <= 0:
        return
    with _READ_CACHE_LOCK:
        _READ_CACHE[key] = (time.monotonic(), value)
        if len(_READ_CACHE) > _READ_CACHE_MAX:
            drop = sorted(_READ_CACHE.items(), key=lambda kv: kv[1][0])[: _READ_CACHE_MAX // 5]
            for k, _ in drop:
                _READ_CACHE.pop(k, None)


def cache_invalidate(prefix: str) -> None:
    with _READ_CACHE_LOCK:
        for key in [k for k in _READ_CACHE if k.startswith(prefix)]:
            _READ_CACHE.pop(key, None)
