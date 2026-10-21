"""Fake `docker` for ops script tests: `docker exec [-i] <container> python ...` runs the
local Python with every `/var/data...` argument mapped into a temp data root."""
from __future__ import annotations

import os
import sys
from pathlib import Path

FAKE_DOCKER = f"""#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
log = Path(os.environ["FAKE_DOCKER_LOG"])
args = sys.argv[1:]
with log.open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:1] == ["inspect"]:
    print(os.environ.get("FAKE_DOCKER_RUNNING", "false"))
    sys.exit(0)
if args[:1] != ["exec"]:
    sys.exit(0)
rest = [a for a in args[1:] if a != "-i"][1:]  # drop -i and the container name
root = os.environ["FAKE_DATA_ROOT"]
mapped = [root + a[len("/var/data"):] if a.startswith("/var/data") else a for a in rest]
mapped[0] = sys.executable if mapped[0] == "python" else mapped[0]
sys.exit(subprocess.run(mapped, env={{**os.environ, "SU_FAKE_ROOT": root}}).returncode)
"""


def install_fake_docker(tmp_path: Path, data_root: Path) -> dict[str, str]:
    """Put a fake docker first on PATH and return the env for running ops scripts."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_DATA_ROOT": str(data_root),
    })
    return env
