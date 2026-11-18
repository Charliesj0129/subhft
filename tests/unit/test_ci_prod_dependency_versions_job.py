"""The advisory CI job that runs the unit suite on the dependency versions production runs.

The engine image is built from the ranges in ``pyproject.toml``, not from ``uv.lock``: the live engine ran pandas 3
and clickhouse-connect 1.x while CI only ever tested the lock's 2.3.3 and 0.10.0. These tests pin the properties
that make the job worth having (the upgrade really happens and survives, it is advisory, the ranges allow it).
"""

from __future__ import annotations

import importlib.metadata
import shlex
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
JOB_ID = "prod-dependency-versions"
PACKAGES = {"pandas": "3.0.0", "clickhouse-connect": "1.5.0"}  # the majors the live engine ran on 2026-10-03
LOCK_VERSIONS = {"pandas": "2.3.3", "clickhouse-connect": "0.10.0"}  # what uv.lock pins today


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


def _upgrade_requirements(job: dict[str, Any]) -> dict[str, Requirement]:
    run = next(run for i, run in _runs(job) if i == _upgrade_index(job))
    tokens = shlex.split(run)
    specs = [tokens[i + 1] for i, token in enumerate(tokens) if token == "--upgrade-package"]
    return {requirement.name: requirement for requirement in map(Requirement, specs)}


def test_both_packages_are_upgraded_through_a_range_that_excludes_the_lock_version() -> None:
    """A bare name left pandas at 2.3.3 in a trial: uv only moves it when the requirement asks for the new major."""
    requirements = _upgrade_requirements(_job())

    assert set(requirements) == set(PACKAGES)
    for package, requirement in requirements.items():
        assert not requirement.specifier.contains(LOCK_VERSIONS[package]), f"{requirement} would not move the package"
        assert requirement.specifier.contains(PACKAGES[package]), f"{requirement} excludes the production version"
        assert "==" not in str(requirement.specifier)  # a range, not a second place to edit a version by hand


def test_the_upgrade_floor_is_the_major_the_job_asserts() -> None:
    job = _job()
    check = next(run for _, run in _runs(job) if "importlib.metadata" in run)

    for package, requirement in _upgrade_requirements(job).items():
        major = int(PACKAGES[package].split(".")[0])
        assert f'"{package}": {major}' in check
        assert not requirement.specifier.contains(f"{major - 1}.99.99"), f"{requirement} allows the previous major"


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


def _pytest_arguments(command: str) -> list[str]:
    tokens = shlex.split(command)
    return [token for token in tokens[tokens.index("pytest") + 1 :] if not token.startswith("--cov")]


def _test_job_recipe() -> str:
    """The command ``test`` runs in CI: the ``test-unit-ci`` Makefile recipe."""
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("test-unit-ci:"))
    recipe = [line.strip() for line in lines[start + 1 :] if line.startswith("\t")]
    return next(line for line in recipe if "pytest" in line)


def test_the_unit_suite_runs_the_whole_tree_with_the_arguments_of_the_test_job() -> None:
    """Same selection, sharding and timeout as ``make test-unit-ci``, minus its coverage options.

    A narrower path (``tests/unit/order``) or a dropped ``-n auto`` would still pass a substring check while testing
    less, or timing out the job, so the argument lists are compared whole.
    """
    run = next(run for i, run in _runs(_job()) if "pytest" in run)

    assert _pytest_arguments(run) == _pytest_arguments(_test_job_recipe())
    assert "tests/unit" in _pytest_arguments(run)


def _version_check_script() -> str:
    run = next(run for _, run in _runs(_job()) if "importlib.metadata" in run)
    return run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]  # the YAML block scalar is already dedented


def _run_version_check(monkeypatch: pytest.MonkeyPatch, installed: dict[str, str]) -> None:
    monkeypatch.setattr(importlib.metadata, "version", installed.__getitem__)
    exec(compile(_version_check_script(), "<ci step>", "exec"), {})  # the repo's own CI step text, not input


def test_the_version_check_accepts_the_production_majors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_version_check(monkeypatch, {"pandas": "3.0.3", "clickhouse-connect": "1.5.0"})  # raises if it rejects them

    assert "3.0.3" in capsys.readouterr().out  # the step also logs what it found, for the run page


@pytest.mark.parametrize(
    ("installed", "named"),
    [
        ({"pandas": "2.3.3", "clickhouse-connect": "1.5.0"}, "pandas"),
        ({"pandas": "3.0.3", "clickhouse-connect": "0.10.0"}, "clickhouse-connect"),
        ({"pandas": "2.3.3", "clickhouse-connect": "0.10.0"}, "pandas"),
    ],
)
def test_the_version_check_fails_when_either_package_is_still_on_the_lock_major(
    monkeypatch: pytest.MonkeyPatch, installed: dict[str, str], named: str
) -> None:
    """The check is what stops a no-op upgrade from turning the job green for the wrong reason."""
    with pytest.raises(AssertionError, match=named):
        _run_version_check(monkeypatch, installed)


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
