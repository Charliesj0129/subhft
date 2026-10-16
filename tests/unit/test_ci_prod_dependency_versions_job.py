"""The advisory CI job that runs the unit suite on the dependency versions production runs.

The engine image is built from the ranges in ``pyproject.toml``, not from ``uv.lock``: the live engine ran pandas 3
and clickhouse-connect 1.x while CI only ever tested the lock's 2.3.3 and 0.10.0. These tests pin the properties
that make the job worth having (the upgrade really happens and survives, it is advisory, the ranges allow it).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
JOB_ID = "prod-dependency-versions"
PACKAGES = {"pandas": "3.0.0", "clickhouse-connect": "1.5.0"}  # the majors the live engine ran on 2026-10-03


def _workflow() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _job() -> dict[str, Any]:
    return _workflow()["jobs"][JOB_ID]


def _runs(job: dict[str, Any]) -> list[tuple[int, str]]:
    return [(i, str(step["run"])) for i, step in enumerate(job["steps"]) if "run" in step]


def _upgrade_index(job: dict[str, Any]) -> int:
    found = [i for i, run in _runs(job) if "uv sync" in run and "--upgrade-package" in run]
    assert len(found) == 1, "exactly one step must upgrade the packages"
    return found[0]


def test_the_job_exists_and_is_advisory() -> None:
    job = _job()

    assert job["continue-on-error"] is True


def test_nothing_waits_for_the_advisory_job() -> None:
    for job_id, job in _workflow()["jobs"].items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        assert JOB_ID not in needs, f"{job_id} would be blocked by an advisory job"


def test_the_job_does_not_wait_for_other_jobs() -> None:
    """Like ``test``: a red lint must not make this signal disappear."""
    assert not _job().get("needs")


def test_both_packages_are_upgraded_by_name_not_pinned_by_hand() -> None:
    run = next(run for i, run in _runs(_job()) if i == _upgrade_index(_job()))

    for package in PACKAGES:
        assert f"--upgrade-package {package}" in run
    assert "==" not in run  # tracks the pyproject ranges instead of a second place to edit


def test_every_command_after_the_upgrade_skips_the_resync_that_would_undo_it() -> None:
    """``uv run`` syncs the environment to uv.lock first, which silently reinstalls pandas 2 and clickhouse-connect 0."""
    job = _job()
    after = [run for i, run in _runs(job) if i > _upgrade_index(job)]

    assert after, "there must be steps that use the upgraded environment"
    for run in after:
        for line in run.splitlines():
            if "uv run" in line:
                assert "--no-sync" in line, line


def test_the_job_asserts_the_production_majors_before_testing() -> None:
    job = _job()
    runs = _runs(job)
    check = [i for i, run in runs if "importlib.metadata" in run and "assert" in run]
    tests = [i for i, run in runs if "pytest" in run]

    assert len(check) == 1 and len(tests) == 1
    assert _upgrade_index(job) < check[0] < tests[0]
    check_run = next(run for i, run in runs if i == check[0])
    assert '"pandas": 3' in check_run and '"clickhouse-connect": 1' in check_run


def test_the_unit_suite_runs_with_the_same_selection_flags_as_the_test_job() -> None:
    job = _job()
    run = next(run for i, run in _runs(job) if "pytest" in run)

    assert "tests/unit" in run
    assert "--timeout=10" in run
    assert "-p no:hypothesis" in run and "-p no:faulthandler" in run


def test_the_job_builds_the_rust_extension_so_failures_are_only_version_effects() -> None:
    uses = [step.get("uses", "") for step in _job()["steps"]]

    assert "./.github/actions/setup-rust" in uses
    assert "./.github/actions/setup-python-uv" in uses


def test_the_checkout_drops_the_token() -> None:
    checkout = next(step for step in _job()["steps"] if str(step.get("uses", "")).startswith("actions/checkout@"))

    assert checkout["with"]["persist-credentials"] is False


@pytest.mark.parametrize(("package", "version"), sorted(PACKAGES.items()))
def test_the_pyproject_range_still_allows_the_production_version(package: str, version: str) -> None:
    """A cap below the tested major would turn the job into a no-op that still looks green."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    requirement = next(Requirement(d) for d in deps if Requirement(d).name == package)

    assert requirement.specifier.contains(version), f"{requirement} excludes {version}"
