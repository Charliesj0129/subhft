"""Least-privilege hygiene for the CI workflows and the ruff per-file-ignores.

A checkout leaves the job's token in ``.git/config`` unless told not to, so every later step of the job can use it.
Only a job that really pushes needs that; the rest drop it.
"""

from __future__ import annotations

import glob
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# (workflow file, job id) pairs whose checkout must keep the token because the job pushes to the remote.
PUSHING_JOBS = {("ci.yml", "benchmark")}


def _load(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), path
    return doc


def _jobs():
    for path in WORKFLOWS:
        doc = _load(path)
        for job_id, job in doc["jobs"].items():
            yield path, doc, job_id, job


def _checkouts(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if str(step.get("uses", "")).startswith("actions/checkout@")]


def test_workflows_were_found() -> None:
    assert len(WORKFLOWS) >= 7
    assert sum(len(_checkouts(job)) for _, _, _, job in _jobs()) >= 19


def test_every_checkout_drops_the_token_unless_the_job_pushes() -> None:
    kept = []
    for path, _, job_id, job in _jobs():
        for step in _checkouts(job):
            persist = (step.get("with") or {}).get("persist-credentials")
            if (path.name, job_id) in PUSHING_JOBS:
                continue
            if persist is not False:
                kept.append(f"{path.name}:{job_id}")
    assert kept == []


def test_the_job_that_keeps_the_token_really_pushes_and_may_write() -> None:
    found = set()
    for path, doc, job_id, job in _jobs():
        if (path.name, job_id) not in PUSHING_JOBS:
            continue
        found.add((path.name, job_id))
        permissions = job.get("permissions") or doc.get("permissions") or {}
        assert permissions.get("contents") == "write", (path.name, job_id)
        assert any("git push" in str(step.get("run", "")) for step in job["steps"]), (path.name, job_id)
        # Without the persisted token the push fails, and the job only warns about it: the baseline would go stale.
        for step in _checkouts(job):
            assert (step.get("with") or {}).get("persist-credentials") is not False, (path.name, job_id)
    assert found == PUSHING_JOBS


def test_every_job_has_explicit_permissions() -> None:
    implicit = [
        f"{path.name}:{job_id}"
        for path, doc, job_id, job in _jobs()
        if "permissions" not in doc and "permissions" not in job
    ]
    assert implicit == []


def test_canary_deploy_workflow_is_read_only() -> None:
    path = ROOT / ".github" / "workflows" / "canary-deploy.yml"
    doc = _load(path)
    for job in doc["jobs"].values():
        permissions = job.get("permissions") or doc.get("permissions")
        assert permissions == {"contents": "read"}


def test_ruff_per_file_ignores_all_match_a_file() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignores = pyproject["tool"]["ruff"]["lint"]["per-file-ignores"]
    unmatched = [pattern for pattern in ignores if not glob.glob(str(ROOT / pattern), recursive=True)]
    assert unmatched == []


def test_pyproject_comments_only_name_existing_agent_files() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    named = {
        match.rstrip(".")
        for line in text.splitlines()
        if line.lstrip().startswith("#")
        for match in re.findall(r"\.agent/[A-Za-z0-9_./-]+", line)
    }
    missing = sorted(name for name in named if not (ROOT / name).exists())
    assert missing == []
