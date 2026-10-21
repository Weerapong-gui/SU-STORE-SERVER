"""Daily SQLite backup, run *inside* su-store-api (the files there belong to the container's root).

usage: python - <db_path> <backup_dir> <keep_days> < backup.py
       python - <db_path> --snapshot <target> < backup.py     (one exact copy, for ops/migrate)
Writes <backup_dir>/orders-<YYYYmmdd-HHMM>.db through the SQLite backup API (safe with WAL),
checks it with PRAGMA integrity_check, records LAST_OK, then deletes backups older than keep_days.
exit: 0 ok · 1 backup or integrity failed (the partial copy is removed)
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ_BANGKOK = timezone(timedelta(hours=7))  # same as core.timeutil; this runs standalone
BACKUP_NAME = re.compile(r"^orders-\d{8}-\d{4}\.db$")


def copy_checked(db_path: Path, target: Path) -> Path:
    """Copy db_path to target (replacing it) and verify the copy; raise on any failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        with sqlite3.connect(db_path) as source, sqlite3.connect(target) as copy:
            source.backup(copy)
            result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"integrity_check: {result}")
    except sqlite3.Error:
        target.unlink(missing_ok=True)
        raise
    return target


def backup(db_path: Path, backup_dir: Path, now: datetime) -> Path:
    return copy_checked(db_path, backup_dir / f"orders-{now:%Y%m%d-%H%M}.db")


def prune(backup_dir: Path, keep_days: int) -> list[str]:
    """Delete only orders-<stamp>.db files older than keep_days; return the removed names."""
    cutoff = time.time() - keep_days * 86400
    removed = []
    for path in sorted(backup_dir.iterdir()):
        if BACKUP_NAME.match(path.name) and path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--snapshot":
        try:
            copy_checked(Path(argv[0]), Path(argv[2]))
        except sqlite3.Error as error:
            print(f"[backup] snapshot ล้มเหลว: {error}", file=sys.stderr)
            return 1
        print(f"[backup] snapshot: {argv[2]}")
        return 0
    db_path, backup_dir, keep_days = Path(argv[0]), Path(argv[1]), int(argv[2])
    now = datetime.now(TZ_BANGKOK).replace(microsecond=0)
    try:
        target = backup(db_path, backup_dir, now)
    except sqlite3.Error as error:
        print(f"[backup] backup ล้มเหลว: {error}", file=sys.stderr)
        return 1
    (backup_dir / "LAST_OK").write_text(now.isoformat() + "\n")
    print(f"[backup] สำเร็จ: {target.name}")
    for name in prune(backup_dir, keep_days):
        print(f"[backup] ลบ backup เก่า: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
