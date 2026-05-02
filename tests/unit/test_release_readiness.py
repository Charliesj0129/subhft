from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "release_readiness.py"
    spec = importlib.util.spec_from_file_location("release_readiness", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_combine_status_escalates_to_fail() -> None:
    mod = _load_module()
    assert mod._combine_status(mod.STATUS_PASS, mod.STATUS_WARN) == mod.STATUS_WARN
    assert mod._combine_status(mod.STATUS_WARN, mod.STATUS_FAIL) == mod.STATUS_FAIL


def test_evaluate_release_readiness_pass() -> None:
    mod = _load_module()
    commands = {
        "git_preconditions": {"returncode": 0},
        "test_hygiene": {"returncode": 0},
        "health_contract": {"returncode": 0},
    }
    result = mod._evaluate_release_readiness(
        commands=commands,
        artifact_sprawl=[],
        missing_make_targets=[],
        missing_ci_markers=[],
        missing_paths=[],
    )
    assert result["overall"] == mod.STATUS_PASS
    assert result["recommendation"] == "canary_ready"


def test_evaluate_release_readiness_fails_on_dirty_repo_signals() -> None:
    mod = _load_module()
    commands = {
        "git_preconditions": {"returncode": 1},
        "test_hygiene": {"returncode": 0},
        "health_contract": {"returncode": 0},
    }
    result = mod._evaluate_release_readiness(
        commands=commands,
        artifact_sprawl=["coverage.json"],
        missing_make_targets=["release-readiness-check"],
        missing_ci_markers=[],
        missing_paths=[],
    )
    assert result["overall"] == mod.STATUS_FAIL
    failed_ids = {check["id"] for check in result["checks"] if check["status"] == mod.STATUS_FAIL}
    assert "git_preconditions_full" in failed_ids
    assert "coverage_artifact_sprawl" in failed_ids
