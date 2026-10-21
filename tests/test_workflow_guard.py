from pathlib import Path

import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _workflows() -> list[dict]:
    return [yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(WORKFLOW_DIR.glob("*.yml"))]


def _triggers(workflow: dict) -> dict:
    # PyYAML's YAML 1.1 resolver reads the unquoted GitHub key `on` as True.
    return workflow.get("on", workflow.get(True, {})) or {}


def _self_hosted_jobs(workflow: dict) -> list[dict]:
    jobs = workflow.get("jobs", {})
    return [
        job
        for job in jobs.values()
        if "self-hosted" in (job.get("runs-on") if isinstance(job.get("runs-on"), list) else [job.get("runs-on")])
    ]


def test_self_hosted_jobs_only_run_on_main_push_or_dispatch() -> None:
    for workflow in _workflows():
        if not _self_hosted_jobs(workflow):
            continue
        triggers = _triggers(workflow)
        assert set(triggers) <= {"push", "workflow_dispatch"}
        assert triggers.get("push", {}).get("branches") == ["main"]


def test_no_pull_request_target_anywhere() -> None:
    for workflow in _workflows():
        assert "pull_request_target" not in _triggers(workflow)


def test_self_hosted_jobs_are_gated() -> None:
    for workflow in _workflows():
        for job in _self_hosted_jobs(workflow):
            assert job.get("environment") == "production"
            assert "vars.DEPLOY_ENABLED == 'true'" in job.get("if", "")


def test_image_is_multi_arch() -> None:
    image_steps = []
    for workflow in _workflows():
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                if step.get("uses") == "docker/build-push-action@v6":
                    image_steps.append(step)
                    assert step.get("with", {}).get("platforms") == "linux/amd64,linux/arm64"
    assert image_steps, "no docker/build-push-action@v6 step found"
