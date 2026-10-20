"""Staff management API and transaction behavior."""
import json
import sqlite3
import threading
from urllib.parse import quote

import pytest
from core import auth, config
from store import db, staff

from .test_store_v2 import _call, server  # noqa: F401 (import pytest fixture)

ROOT, ROOT_PASSWORD = "root", "pw-root"


def _login(base, username="staff1", password="pw"):
    status, body = _call(base, "POST", "/v2/admin/login", {"username": username, "password": password})
    assert status == 200, body
    return json.loads(body)["token"]


def _login_root(base):
    with sqlite3.connect(config.DB_PATH) as conn:
        _insert_user(conn, ROOT, ROOT_PASSWORD, True)
    return _login(base, ROOT, ROOT_PASSWORD)


def _insert_user(conn, username, password, is_superadmin, created_by="test"):
    conn.execute(
        "INSERT INTO admin_users "
        "(username, claim_token, super_token, is_superadmin, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            username,
            auth.compute_claim_token(username, password),
            auth.compute_super_token(username, password) if is_superadmin else None,
            int(is_superadmin),
            created_by,
            "2026-10-20T00:00:00+07:00",
        ),
    )


def _staff_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_store_db(conn)
    conn.execute(
        "CREATE TABLE admin_users (username TEXT PRIMARY KEY, claim_token TEXT NOT NULL, super_token TEXT, "
        "is_superadmin INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL)"
    )
    return conn


def test_create_returns_password_once_and_it_logs_in(server):  # noqa: F811 (imported pytest fixture)
    token = _login_root(server)
    headers = {"Authorization": f"Claim {token}"}
    status, body = _call(server, "POST", "/v2/admin/users", {"username": "new.staff"}, headers)
    assert status == 200
    result = json.loads(body)
    assert len(result["password"]) == 12
    assert _call(server, "POST", "/v2/admin/login", {"username": "new.staff", "password": result["password"]})[0] == 200
    status, body = _call(server, "GET", "/v2/admin/users", headers=headers)
    listed = json.loads(body)["users"]
    assert status == 200
    assert all("password" not in user and "token" not in user for user in listed)


def test_non_superadmin_gets_403_for_each_staff_action(server):  # noqa: F811 (imported pytest fixture)
    headers = {"Authorization": f"Claim {_login(server)}"}
    requests = [
        ("GET", "/v2/admin/users", None),
        ("POST", "/v2/admin/users", {"username": "newuser"}),
        ("POST", "/v2/admin/users/staff1/reset-password", {}),
        ("PATCH", "/v2/admin/users/staff1", {"isSuperadmin": True}),
        ("DELETE", "/v2/admin/users/staff1", None),
    ]
    for method, path, body in requests:
        assert _call(server, method, path, body, headers)[0] == 403


def test_reset_password_invalidates_old_session(server):  # noqa: F811 (imported pytest fixture)
    with sqlite3.connect(config.DB_PATH) as conn:
        _insert_user(conn, "root", "old-password", True)
    old_token = _login(server, "root", "old-password")
    status, body = _call(
        server, "POST", "/v2/admin/users/root/reset-password", {}, {"Authorization": f"Claim {old_token}"}
    )
    assert status == 200
    assert _call(server, "GET", "/v2/admin/me", headers={"Authorization": f"Claim {old_token}"})[0] == 401
    assert _call(server, "POST", "/v2/admin/login", {"username": "root", "password": json.loads(body)["password"]})[0] == 200


def test_delete_invalidates_session_and_blocks_self(server):  # noqa: F811 (imported pytest fixture)
    with sqlite3.connect(config.DB_PATH) as conn:
        _insert_user(conn, "root", "pw-root", True)
        _insert_user(conn, "other", "pw-other", False, "root")
    root_token = _login(server, "root", "pw-root")
    headers = {"Authorization": f"Claim {root_token}"}
    assert _call(server, "DELETE", "/v2/admin/users/root", headers=headers)[0] == 403
    other_token = _login(server, "other", "pw-other")
    status, _ = _call(server, "DELETE", "/v2/admin/users/other", headers=headers)
    assert status == 200
    assert _call(server, "GET", "/v2/admin/me", headers={"Authorization": f"Claim {other_token}"})[0] == 401


def test_last_superadmin_cannot_be_demoted_or_deleted(server):  # noqa: F811 (imported pytest fixture)
    with sqlite3.connect(config.DB_PATH) as conn:
        _insert_user(conn, "root", "pw-root", True)
    headers = {"Authorization": f"Claim {_login(server, 'root', 'pw-root')}"}
    assert _call(server, "PATCH", "/v2/admin/users/root", {"isSuperadmin": False}, headers)[0] == 409
    # The root account cannot delete itself (403 first), so delete through the shared API token.
    token_headers = {"Authorization": "Bearer test-token"}
    assert _call(server, "DELETE", "/v2/admin/users/root", headers=token_headers)[0] == 409
    assert _call(server, "DELETE", "/v2/admin/users/staff1", headers=token_headers)[0] == 200


def test_concurrent_demotion_keeps_one_superadmin(tmp_path):
    path = str(tmp_path / "staff.db")
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.ensure_store_db(conn)
    conn.execute(
        "CREATE TABLE admin_users (username TEXT PRIMARY KEY, claim_token TEXT NOT NULL, super_token TEXT, "
        "is_superadmin INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL DEFAULT 'system', created_at TEXT NOT NULL)"
    )
    for username in ("root1", "root2"):
        _insert_user(conn, username, "pw", True)
    conn.commit()
    outcomes = []

    def demote(username, actor):
        local = sqlite3.connect(path, timeout=30, check_same_thread=False)
        local.row_factory = sqlite3.Row
        try:
            staff.set_superadmin(local, username, False, actor)
            outcomes.append("ok")
        except db.StoreError as error:
            outcomes.append(error.status)
        finally:
            local.close()

    threads = [threading.Thread(target=demote, args=(username, "other")) for username in ("root1", "root2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert conn.execute("SELECT COUNT(*) FROM admin_users WHERE is_superadmin = 1").fetchone()[0] == 1
    conn.close()


def test_legacy_thai_username_is_reachable(server):  # noqa: F811 (imported pytest fixture)
    with sqlite3.connect(config.DB_PATH) as conn:
        _insert_user(conn, "สตาฟ หนึ่ง", "pw", False)
        _insert_user(conn, "root", "pw-root", True)
    headers = {"Authorization": f"Claim {_login(server, 'root', 'pw-root')}"}
    assert _call(server, "DELETE", f"/v2/admin/users/{quote('สตาฟ หนึ่ง')}", headers=headers)[0] == 200


@pytest.mark.parametrize("username", ["a b", "x" * 65])
def test_username_rules_reject_invalid_name(server, username):  # noqa: F811 (imported pytest fixture)
    headers = {"Authorization": f"Claim {_login_root(server)}"}
    status, body = _call(server, "POST", "/v2/admin/users", {"username": username}, headers)
    assert status == 400
    assert json.loads(body)["message"] == "ชื่อผู้ใช้ใช้ได้เฉพาะ a-z A-Z 0-9 . _ - ไม่เกิน 64 ตัว"


def test_username_rules_reject_duplicate(server):  # noqa: F811 (imported pytest fixture)
    with sqlite3.connect(config.DB_PATH) as conn:
        _insert_user(conn, "root", "pw-root", True)
    headers = {"Authorization": f"Claim {_login(server, 'root', 'pw-root')}"}
    status, body = _call(server, "POST", "/v2/admin/users", {"username": "staff1"}, headers)
    assert status == 409
    assert json.loads(body)["message"] == "ชื่อผู้ใช้นี้มีอยู่แล้ว"


def test_actions_are_audited(server):  # noqa: F811 (imported pytest fixture)
    with sqlite3.connect(config.DB_PATH) as conn:
        _insert_user(conn, "root", "pw-root", True)
    headers = {"Authorization": f"Claim {_login(server, 'root', 'pw-root')}"}
    _call(server, "POST", "/v2/admin/users", {"username": "created"}, headers)
    _call(server, "POST", "/v2/admin/users/created/reset-password", {}, headers)
    _call(server, "PATCH", "/v2/admin/users/created", {"isSuperadmin": True}, headers)
    _call(server, "PATCH", "/v2/admin/users/created", {"isSuperadmin": False}, headers)
    _call(server, "DELETE", "/v2/admin/users/created", headers=headers)
    with sqlite3.connect(config.DB_PATH) as conn:
        events = {row[0] for row in conn.execute("SELECT event FROM order_audit_log")}
    assert {"v2_staff_created", "v2_staff_password_reset", "v2_staff_superadmin_on",
            "v2_staff_superadmin_off", "v2_staff_deleted"} <= events


def test_list_staff_orders_superadmins_before_created_at():
    conn = _staff_conn()
    _insert_user(conn, "staff-old", "pw", False)
    _insert_user(conn, "root-late", "pw", True)
    _insert_user(conn, "staff-new", "pw", False)
    conn.execute("UPDATE admin_users SET created_at = '2026-10-21T00:00:00+07:00' WHERE username = 'staff-new'")
    conn.execute("UPDATE admin_users SET created_at = '2026-10-22T00:00:00+07:00' WHERE username = 'root-late'")
    members = staff.list_staff(conn)
    assert [member["username"] for member in members] == ["root-late", "staff-old", "staff-new"]


def test_username_validation_and_missing_user_errors():
    conn = _staff_conn()
    with pytest.raises(db.StoreError) as error:
        staff.create_staff(conn, "a b", "root")
    assert error.value.status == 400
    with pytest.raises(db.StoreError) as error:
        staff.reset_password(conn, "missing")
    assert error.value.status == 404
