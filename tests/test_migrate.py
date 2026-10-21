"""ops/migrate: counts/compare (counts.py), import-new.sh, verify.sh, export-old.sh."""
from __future__ import annotations

import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from store import db

from .ops_fakes import install_fake_docker
from .test_store_v2 import CUSTOMER, _conn, _product

OPS = Path(__file__).resolve().parents[1] / "ops"
MIGRATE = OPS / "migrate"
COUNTS = MIGRATE / "counts.py"


def _make_db(path: Path, *, slips: list[str]) -> None:
    """A store DB with one product, one order per slip path, and the shared admin_users table."""
    conn = _conn(str(path))
    conn.execute("CREATE TABLE admin_users (username TEXT PRIMARY KEY, claim_token TEXT NOT NULL, "
                 "super_token TEXT, is_superadmin INTEGER NOT NULL DEFAULT 0, "
                 "created_by TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO admin_users (username, claim_token, created_at) VALUES ('staff', 't', 'now')")
    variant = _product(conn, stock=None)["variants"][0]["id"]
    for slip in slips:
        code = db.create_order(conn, {"items": [{"variantId": variant, "quantity": 1}], "customer": CUSTOMER})["orderCode"]
        conn.execute("UPDATE store_orders SET slip_storage_path = ? WHERE order_code = ?", (slip, code))
    db.create_order(conn, {"items": [{"variantId": variant, "quantity": 1}], "customer": CUSTOMER})
    conn.commit()
    conn.close()


def _counts(*args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    sys.argv = [str(COUNTS), *args]
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(COUNTS), run_name="__main__")
    out, err = capsys.readouterr()
    return exit_info.value.code, out, err


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "srv" / "data" / "order-api"
    (root / "su-order-api" / "slips").mkdir(parents=True)
    return root


SLIP = "/var/data/su-order-api/slips/SU2610-0001.jpg"


def test_counts_reports_tables_counters_and_slips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_db(tmp_path / "o.db", slips=[SLIP])
    code, out, _ = _counts(str(tmp_path / "o.db"), capsys=capsys)
    counts = json.loads(out)
    assert code == 0
    assert counts["tables"]["store_orders"] == 2
    assert counts["tables"]["admin_users"] == 1
    assert counts["slips"] == 1
    assert list(counts["counters"].values()) == [2]  # one monthly order period, two orders
    assert "missing_slips" not in counts


def test_counts_lists_missing_slip_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _data_root(tmp_path)
    _make_db(tmp_path / "o.db", slips=[SLIP, "/var/data/su-order-api/slips/missing.jpg"])
    (root / "su-order-api" / "slips" / "SU2610-0001.jpg").write_bytes(b"jpg")
    code, out, _ = _counts(str(tmp_path / "o.db"), "--data-root", str(root), capsys=capsys)
    assert code == 0
    assert json.loads(out)["missing_slips"] == ["/var/data/su-order-api/slips/missing.jpg"]


def test_counts_opens_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_db(tmp_path / "o.db", slips=[])
    before = sorted(p.name for p in tmp_path.iterdir()), (tmp_path / "o.db").stat().st_mtime_ns
    _counts(str(tmp_path / "o.db"), capsys=capsys)
    assert (sorted(p.name for p in tmp_path.iterdir()), (tmp_path / "o.db").stat().st_mtime_ns) == before


def test_list_slips_prints_paths_relative_to_var_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_db(tmp_path / "o.db", slips=[SLIP])
    code, out, _ = _counts(str(tmp_path / "o.db"), "--list-slips", capsys=capsys)
    assert code == 0
    assert out.splitlines() == ["su-order-api/slips/SU2610-0001.jpg"]


def _compare(tmp_path: Path, old: dict, capsys: pytest.CaptureFixture[str], root: Path | None = None):
    (tmp_path / "old.json").write_text(json.dumps(old))
    args = [str(tmp_path / "o.db"), "--compare", str(tmp_path / "old.json")]
    if root:
        args += ["--data-root", str(root)]
    return _counts(*args, capsys=capsys)


def test_compare_passes_when_everything_matches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _data_root(tmp_path)
    _make_db(tmp_path / "o.db", slips=[SLIP])
    (root / "su-order-api" / "slips" / "SU2610-0001.jpg").write_bytes(b"jpg")
    _, old_json, _ = _counts(str(tmp_path / "o.db"), capsys=capsys)
    code, out, _ = _compare(tmp_path, json.loads(old_json), capsys, root)
    assert code == 0, out
    assert "ตรงกันทุกค่า" in out


def test_compare_fails_on_counter_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _data_root(tmp_path)
    _make_db(tmp_path / "o.db", slips=[])
    _, old_json, _ = _counts(str(tmp_path / "o.db"), capsys=capsys)
    old = json.loads(old_json)
    old["counters"]["receipt"] = 41
    code, out, _ = _compare(tmp_path, old, capsys, root)
    assert code == 1
    assert "counters.receipt" in out


def test_compare_fails_on_missing_slip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _data_root(tmp_path)
    _make_db(tmp_path / "o.db", slips=[SLIP])
    _, old_json, _ = _counts(str(tmp_path / "o.db"), capsys=capsys)
    code, out, _ = _compare(tmp_path, json.loads(old_json), capsys, root)
    assert code == 1
    assert SLIP in out


# --- shell wrappers against fake docker / rsync / ssh -------------------------------------


def _sh(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(MIGRATE / script), *args], env=env, text=True, capture_output=True, timeout=60)


def _new_host(tmp_path: Path) -> tuple[dict[str, str], Path]:
    root = _data_root(tmp_path)
    env = install_fake_docker(tmp_path, root)
    env["SU_STORE_DIR"] = str(tmp_path / "srv")
    (tmp_path / "srv" / "backend").mkdir(parents=True)
    (tmp_path / "srv" / "backend" / "REVISION").write_text("a" * 40 + "\n")
    return env, root


def _docker_calls(env: dict[str, str]) -> list[list[str]]:
    log = Path(env["FAKE_DOCKER_LOG"])
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_import_refuses_while_api_running(tmp_path: Path) -> None:
    env, _ = _new_host(tmp_path)
    env["FAKE_DOCKER_RUNNING"] = "true"
    result = _sh("import-new.sh", env)
    assert result.returncode == 1
    assert "su-store-api ยังรันอยู่" in result.stderr
    assert not any(call[0] == "run" for call in _docker_calls(env))


def test_import_requires_incoming_files(tmp_path: Path) -> None:
    env, _ = _new_host(tmp_path)
    result = _sh("import-new.sh", env)
    assert result.returncode == 1
    assert "incoming" in result.stderr


def test_import_keeps_previous_db_beside_new_one(tmp_path: Path) -> None:
    env, root = _new_host(tmp_path)
    live = root / "su-order-api"
    (live / "orders.db").write_text("old db")
    (live / "orders.db-wal").write_text("old wal")
    (live / "slips" / "kept.jpg").write_text("existing slip")
    incoming = root / "incoming"
    (incoming / "su-order-api" / "slips").mkdir(parents=True)
    (incoming / "su-order-api" / "product-images").mkdir(parents=True)
    (incoming / "orders.db").write_text("new db")
    (incoming / "counts.json").write_text("{}")
    (incoming / "su-order-api" / "slips" / "kept.jpg").write_text("must not overwrite")
    (incoming / "su-order-api" / "slips" / "new.jpg").write_text("new slip")
    (incoming / "su-order-api" / "product-images" / "p.webp").write_text("img")
    result = _sh("import-new.sh", env)
    assert result.returncode == 0, result.stderr
    assert (live / "orders.db").read_text() == "new db"
    assert not (live / "orders.db-wal").exists()
    replaced = sorted(p.name for p in live.glob("orders.db*.replaced-*"))
    assert {name.split(".replaced-")[0] for name in replaced} == {"orders.db", "orders.db-wal"}
    assert (live / "slips" / "kept.jpg").read_text() == "existing slip"
    assert (live / "slips" / "new.jpg").read_text() == "new slip"
    assert (live / "product-images" / "p.webp").read_text() == "img"
    (run,) = [call for call in _docker_calls(env) if call[0] == "run"]
    assert f"ghcr.io/weerapong-gui/su-store-server:{'a' * 40}" in run


def test_verify_sh_compares_inside_the_container(tmp_path: Path) -> None:
    env, root = _new_host(tmp_path)
    _make_db(root / "su-order-api" / "orders.db", slips=[])
    (root / "incoming").mkdir()
    old = subprocess.run([sys.executable, str(COUNTS), str(root / "su-order-api" / "orders.db")],
                         capture_output=True, text=True, check=True).stdout
    (root / "incoming" / "counts.json").write_text(old)
    assert _sh("verify.sh", env).returncode == 0
    broken = json.loads(old)
    broken["tables"]["store_orders"] += 1
    (root / "incoming" / "counts.json").write_text(json.dumps(broken))
    result = _sh("verify.sh", env)
    assert result.returncode == 1
    assert "tables.store_orders" in result.stdout
    assert any(call[:3] == ["exec", "-i", "su-store-api"] for call in _docker_calls(env))


FAKE_RECORDER = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_CMD_LOG"], "a") as handle:
    handle.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
if os.path.basename(sys.argv[0]) == "rsync":
    for arg in sys.argv[1:]:
        if arg.startswith("--files-from="):
            with open(arg.split("=", 1)[1]) as files, open(os.environ["FAKE_FILES_FROM"], "w") as out:
                out.write(files.read())
"""


def test_export_sends_db_counts_and_only_su_slips_without_delete(tmp_path: Path) -> None:
    old_root = tmp_path / "old" / "order-api"
    (old_root / "su-order-api" / "slips").mkdir(parents=True)
    (old_root / "su-order-api" / "product-images").mkdir()
    _make_db(old_root / "su-order-api" / "orders.db", slips=[SLIP])
    (old_root / "su-order-api" / "slips" / "SU2610-0001.jpg").write_text("su")
    (old_root / "su-order-api" / "slips" / "FP2801501.jpg").write_text("fp28")
    env = install_fake_docker(tmp_path, old_root)
    bin_dir = Path(env["PATH"].split(os.pathsep)[0])
    for name in ("rsync", "ssh"):
        (bin_dir / name).write_text(FAKE_RECORDER)
        (bin_dir / name).chmod(0o755)
    env.update({"OLD_DATA_DIR": str(old_root), "NEW_HOST": "su-runner@new-host",
                "FAKE_CMD_LOG": str(tmp_path / "cmd.log"), "FAKE_FILES_FROM": str(tmp_path / "files.txt")})
    result = _sh("export-old.sh", env, str(tmp_path / "snapshot"))
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (tmp_path / "cmd.log").read_text().splitlines()]
    rsyncs = [call for call in calls if call[0] == "rsync"]
    assert rsyncs and all("--delete" not in " ".join(call) for call in rsyncs)
    assert all(call[-1].startswith("su-runner@new-host:") for call in rsyncs)
    assert (tmp_path / "files.txt").read_text().split() == ["su-order-api/slips/SU2610-0001.jpg"]
    counts = json.loads((tmp_path / "snapshot" / "counts.json").read_text())
    assert counts["tables"]["store_orders"] == 2
    with sqlite3.connect(old_root / "su-store-migrate" / "orders.db") as snapshot:
        assert snapshot.execute("SELECT COUNT(*) FROM store_orders").fetchone()[0] == 2
