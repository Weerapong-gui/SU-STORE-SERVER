"""ops/backup.sh + ops/backup.py: consistent SQLite copy, integrity check, retention."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path

from .ops_fakes import install_fake_docker

OPS = Path(__file__).resolve().parents[1] / "ops"


def _setup(tmp_path: Path) -> tuple[dict[str, str], Path]:
    data_root = tmp_path / "srv" / "data" / "order-api"
    (data_root / "su-order-api").mkdir(parents=True)
    with sqlite3.connect(data_root / "su-order-api" / "orders.db") as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE store_orders (order_code TEXT)")
        conn.execute("INSERT INTO store_orders VALUES ('SU2610-0001')")
    env = install_fake_docker(tmp_path, data_root)
    env["SU_STORE_DIR"] = str(tmp_path / "srv")
    return env, data_root / "backups"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(OPS / "backup.sh")], env=env, text=True, capture_output=True, timeout=30)


def test_backup_creates_consistent_copy_and_last_ok(tmp_path: Path) -> None:
    env, backups = _setup(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    (copy,) = backups.glob("orders-*.db")
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT order_code FROM store_orders").fetchall() == [("SU2610-0001",)]
    assert (backups / "LAST_OK").read_text().strip().endswith("+07:00")


def test_corrupt_copy_is_removed_and_fails(tmp_path: Path) -> None:
    env, backups = _setup(tmp_path)
    db = Path(env["FAKE_DATA_ROOT"]) / "su-order-api" / "orders.db"
    for leftover in db.parent.glob("orders.db-*"):
        leftover.unlink()
    db.write_bytes(b"not a sqlite database" * 100)
    result = _run(env)
    assert result.returncode == 1
    assert "backup ล้มเหลว" in result.stderr
    assert list(backups.glob("orders-*.db")) == []
    assert not (backups / "LAST_OK").exists()


def test_retention_deletes_only_old_backup_files(tmp_path: Path) -> None:
    env, backups = _setup(tmp_path)
    backups.mkdir()
    old = time.time() - 15 * 86400
    keep_recent = backups / "orders-20261019-0400.db"
    for name in ("orders-20261001-0400.db", "note.txt", "orders-manual.db"):
        (backups / name).write_text("x")
        os.utime(backups / name, (old, old))
    keep_recent.write_text("x")
    result = _run(env)
    assert result.returncode == 0, result.stderr
    names = {p.name for p in backups.iterdir()}
    assert "orders-20261001-0400.db" not in names
    assert {"note.txt", "orders-manual.db", keep_recent.name, "LAST_OK"} <= names


def test_missing_su_store_dir_exits_64(tmp_path: Path) -> None:
    env, _ = _setup(tmp_path)
    del env["SU_STORE_DIR"]
    result = _run(env)
    assert result.returncode == 64
    assert "[backup]" in result.stderr


def test_snapshot_mode_writes_one_exact_copy(tmp_path: Path) -> None:
    env, backups = _setup(tmp_path)
    root = Path(env["FAKE_DATA_ROOT"])
    target = root / "su-store-migrate" / "orders.db"
    target.parent.mkdir()
    target.write_text("stale snapshot")
    result = subprocess.run(
        ["docker", "exec", "-i", "su-store-api", "python", "-", "/var/data/su-order-api/orders.db",
         "--snapshot", "/var/data/su-store-migrate/orders.db"],
        stdin=(OPS / "backup.py").open(), env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM store_orders").fetchone()[0] == 1
    assert not backups.exists()
