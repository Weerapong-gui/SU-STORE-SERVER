"""ops/deploy-service.sh against fake docker/flock binaries (no real containers)."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "deploy-service.sh"
NEW_SHA = "a" * 40
OLD_SHA = "b" * 40
SERVER_IMAGE = "ghcr.io/weerapong-gui/su-store-server"

# Health is looked up by the tag of the last `up`, so rollback scenarios don't depend on timing.
FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ["FAKE_DOCKER_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
tag = os.environ.get("API_IMAGE_TAG") or os.environ.get("WEB_IMAGE_TAG")
state["calls"].append({"args": args, "tag": tag})
code = 0
if args[0] == "pull" and state.get("pull_fails"):
    code = 1
elif args[:2] == ["compose", "-f"] and "up" in args:
    state["running"] = tag
elif args[0] == "inspect":
    print(state["health"].get(state.get("running"), "starting"))
elif args[:2] == ["image", "ls"]:
    print("\\n".join(state["tags"]))
elif args[:2] == ["image", "rm"] and state.get("rm_fails"):
    code = 1
path.write_text(json.dumps(state))
sys.exit(code)
"""

# Real flock(1) semantics for the `flock -w <seconds> <fd>` form the script uses, so the
# lock test also runs on macOS, which ships no flock.
FAKE_FLOCK = """#!/usr/bin/env python3
import fcntl, sys, time
timeout, fd = float(sys.argv[2]), int(sys.argv[3])
deadline = time.monotonic() + timeout
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.05)
"""


def _setup(tmp_path: Path, health: dict[str, str], **state: object) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("flock", FAKE_FLOCK)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"health": health, "tags": [], "calls": [], **state}))
    host = tmp_path / "host"
    for part in ("backend", "frontend"):
        (host / part).mkdir(parents=True)
        (host / part / "compose.yml").touch()
        (host / part / ".env").touch()
    env = {k: v for k, v in os.environ.items() if k not in ("API_IMAGE_TAG", "WEB_IMAGE_TAG")}
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_DOCKER_STATE": str(state_path),
        "SU_STORE_DIR": str(host),
        "DEPLOY_POLL_SECONDS": "0",
        "DEPLOY_HEALTH_TIMEOUT": "1",
    })
    return env, state_path


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, text=True, capture_output=True, timeout=30)


def _calls(state_path: Path) -> list[dict]:
    return json.loads(state_path.read_text())["calls"]


def _ups(state_path: Path) -> list[dict]:
    return [call for call in _calls(state_path) if "up" in call["args"]]


def _revision(env: dict[str, str], part: str = "backend") -> Path:
    return Path(env["SU_STORE_DIR"]) / part / "REVISION"


def test_healthy_deploy_writes_revision(tmp_path: Path) -> None:
    env, state_path = _setup(tmp_path, {NEW_SHA: "healthy"})
    result = _run(env, "backend", NEW_SHA)
    assert result.returncode == 0, result.stderr
    assert _revision(env).read_text().strip() == NEW_SHA
    (up,) = _ups(state_path)
    assert up["args"][-4:] == ["up", "-d", "--no-deps", "su-store-api"] and up["tag"] == NEW_SHA


def test_frontend_deploy_uses_web_service_and_image(tmp_path: Path) -> None:
    env, state_path = _setup(tmp_path, {NEW_SHA: "healthy"})
    result = _run(env, "frontend", NEW_SHA)
    assert result.returncode == 0, result.stderr
    assert {"args": ["pull", f"ghcr.io/weerapong-gui/su-store-web:{NEW_SHA}"], "tag": None} in _calls(state_path)
    (up,) = _ups(state_path)
    assert up["args"][-1] == "su-store-web" and up["tag"] == NEW_SHA
    assert _revision(env, "frontend").read_text().strip() == NEW_SHA


def test_unhealthy_rolls_back_to_previous(tmp_path: Path) -> None:
    env, state_path = _setup(tmp_path, {NEW_SHA: "unhealthy", OLD_SHA: "healthy"})
    _revision(env).write_text(f"{OLD_SHA}\n")
    result = _run(env, "backend", NEW_SHA)
    assert result.returncode == 1
    assert [up["tag"] for up in _ups(state_path)] == [NEW_SHA, OLD_SHA]
    assert _revision(env).read_text() == f"{OLD_SHA}\n"
    assert "ย้อนกลับไป" in result.stderr and "สำเร็จ" in result.stderr


def test_rollback_that_stays_unhealthy_says_so(tmp_path: Path) -> None:
    env, _ = _setup(tmp_path, {NEW_SHA: "unhealthy", OLD_SHA: "unhealthy"})
    _revision(env).write_text(f"{OLD_SHA}\n")
    result = _run(env, "backend", NEW_SHA)
    assert result.returncode == 1
    assert "ยังไม่ healthy" in result.stderr


def test_first_deploy_unhealthy_does_not_roll_back(tmp_path: Path) -> None:
    env, state_path = _setup(tmp_path, {NEW_SHA: "unhealthy"})
    result = _run(env, "backend", NEW_SHA)
    assert result.returncode == 2
    assert len(_ups(state_path)) == 1
    assert not _revision(env).exists()


def test_missing_image_exits_4_without_touching_the_service(tmp_path: Path) -> None:
    env, state_path = _setup(tmp_path, {}, pull_fails=True)
    result = _run(env, "backend", NEW_SHA)
    assert result.returncode == 4
    assert "ดึง image" in result.stderr
    assert _ups(state_path) == []


def test_keeps_three_newest_tags(tmp_path: Path) -> None:
    tags = [NEW_SHA, OLD_SHA, "c" * 40, "d" * 40, "e" * 40, "<none>"]
    env, state_path = _setup(tmp_path, {NEW_SHA: "healthy"}, tags=tags)
    _revision(env).write_text(f"{OLD_SHA}\n")
    result = _run(env, "backend", NEW_SHA)
    removed = [call["args"][-1] for call in _calls(state_path) if call["args"][:2] == ["image", "rm"]]
    assert result.returncode == 0, result.stderr
    assert removed == [f"{SERVER_IMAGE}:{'d' * 40}", f"{SERVER_IMAGE}:{'e' * 40}"]


def test_failed_cleanup_keeps_a_good_deploy_green(tmp_path: Path) -> None:
    tags = [NEW_SHA, "c" * 40, "d" * 40, "e" * 40]
    env, _ = _setup(tmp_path, {NEW_SHA: "healthy"}, tags=tags, rm_fails=True)
    result = _run(env, "backend", NEW_SHA)
    assert result.returncode == 0, result.stderr
    assert _revision(env).read_text().strip() == NEW_SHA


def test_never_prunes(tmp_path: Path) -> None:
    env, state_path = _setup(tmp_path, {NEW_SHA: "healthy"}, tags=[NEW_SHA, "c" * 40, "d" * 40, "e" * 40])
    _run(env, "backend", NEW_SHA)
    assert all("prune" not in call["args"] for call in _calls(state_path))


def test_lock_timeout_exits_3(tmp_path: Path) -> None:
    env, state_path = _setup(tmp_path, {NEW_SHA: "healthy"})
    env["DEPLOY_LOCK_TIMEOUT"] = "1"
    with (Path(env["SU_STORE_DIR"]) / ".deploy.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = _run(env, "frontend", NEW_SHA)
    assert result.returncode == 3
    assert "deploy อื่นกำลังรัน" in result.stderr
    assert _calls(state_path) == []


@pytest.mark.parametrize("args", [("other", NEW_SHA), ("backend", "not-a-sha"), ("backend", NEW_SHA.upper())])
def test_bad_args_exit_64(tmp_path: Path, args: tuple[str, str]) -> None:
    env, state_path = _setup(tmp_path, {})
    result = _run(env, *args)
    assert result.returncode == 64
    assert f"[deploy {args[0]}]" in result.stderr
    assert _calls(state_path) == []
