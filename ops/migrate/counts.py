"""Fingerprint a store DB for the move between hosts, and compare two fingerprints.

Runs with only the stdlib: on the old host and new host it is fed to the order-api container's
Python (`docker exec -i <container> python - ... < counts.py`), since neither host has jq.

usage:
  counts.py <db>                                   JSON fingerprint on stdout
  counts.py <db> --data-root <dir>                 + missing_slips (slip files absent under <dir>)
  counts.py <db> --list-slips                      slip paths relative to /var/data, one per line
  counts.py <db> --compare <old.json> [--data-root <dir>]
                                                   table of old vs new; exit 1 on any difference
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Slip paths are stored as full container paths (core/slips.py); /var/data is the data mount.
CONTAINER_DATA = "/var/data"
TABLES = ("admin_users", "store_categories", "store_order_items", "store_orders",
          "store_products", "store_receipts", "store_variants")


def _connect(db_path: str) -> contextlib.closing[sqlite3.Connection]:
    # Read-only, and immutable is NOT set: the live DB may be in WAL mode with readers active.
    return contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True))


def slip_paths(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT slip_storage_path FROM store_orders WHERE slip_storage_path IS NOT NULL ORDER BY id"
    ).fetchall()
    return [row[0] for row in rows]


def fingerprint(db_path: str, data_root: str | None = None) -> dict[str, Any]:
    with _connect(db_path) as conn:
        result: dict[str, Any] = {
            "tables": {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES},
            "counters": dict(conn.execute("SELECT period, last_seq FROM store_counters ORDER BY period")),
            "slips": len(slip_paths(conn)),
        }
        if data_root is not None:
            result["missing_slips"] = [
                path for path in slip_paths(conn)
                if not Path(data_root + path.removeprefix(CONTAINER_DATA)).is_file()
            ]
    return result


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        return {k: v for key, item in value.items() for k, v in _flatten(item, f"{prefix}{key}.").items()}
    return {prefix.rstrip("."): value}


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Print old vs new for every key; return the keys that differ (plus any missing slips)."""
    missing = new.pop("missing_slips", [])
    old_flat, new_flat = _flatten(old), _flatten(new)
    differences = []
    for key in sorted(old_flat.keys() | new_flat.keys()):
        mark = "" if old_flat.get(key) == new_flat.get(key) else "  <-- ไม่ตรง"
        if mark:
            differences.append(key)
        print(f"{key:<32} เก่า={old_flat.get(key)!s:<10} ใหม่={new_flat.get(key)!s:<10}{mark}")
    for path in missing:
        print(f"สลิปหาย: {path}")
    return differences + missing


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="fingerprint/compare a store DB")
    parser.add_argument("db")
    parser.add_argument("--data-root")
    parser.add_argument("--list-slips", action="store_true")
    parser.add_argument("--compare", metavar="OLD_JSON")
    args = parser.parse_args(argv)
    if args.list_slips:
        with _connect(args.db) as conn:
            for path in slip_paths(conn):
                print(path.removeprefix(CONTAINER_DATA + "/"))
        return 0
    new = fingerprint(args.db, args.data_root)
    if args.compare is None:
        print(json.dumps(new, ensure_ascii=False, sort_keys=True))
        return 0
    differences = compare(json.loads(Path(args.compare).read_text(encoding="utf-8")), new)
    if differences:
        print(f"ไม่ตรงกัน {len(differences)} รายการ: หยุด cutover แล้วเปิดร้านที่เครื่องเก่าตามเดิม")
        return 1
    print("ตรงกันทุกค่า และสลิปครบทุกไฟล์")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
