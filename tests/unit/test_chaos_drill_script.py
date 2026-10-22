"""``scripts/run-chaos-drill.sh`` keeps its log where the sign-off can find it later.

The log is the evidence the runbook asks the operator to review and to attach to a failure issue. It used to be
written to ``/tmp``, which a host cleans on its own schedule, so a quarterly drill's record could be gone before
anyone looked. The log now goes to ``<repo>/outputs/drills/``. These tests run the real script in a temporary
repository layout with a fake ``uv``, so no playbook runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "run-chaos-drill.sh"
RUNBOOK = REPO / "docs" / "runbooks" / "quarterly-chaos-drill.md"
PLAYBOOKS = ("broker_disconnect", "clickhouse_down", "feed_gap", "position_drift", "disk_full")

_FAKE_UV = """#!/usr/bin/env bash
echo "fake uv: $*"
echo "$(pwd)" >> "$FAKE_UV_CWD_LOG"
case "$*" in
  *"test_playbook_${FAKE_UV_FAIL:-none}.py"*) exit 1 ;;
esac
exit 0
"""


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """A repository-shaped directory holding a copy of the script, plus a ``bin`` with a fake ``uv``."""
    tmp_path = tmp_path.resolve()  # the script prints physical paths
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "run-chaos-drill.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(_FAKE_UV)
    fake.chmod(0o755)
    return tmp_path


def _run(sandbox: Path, *, fail: str = "", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{sandbox / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(sandbox),
        "FAKE_UV_CWD_LOG": str(sandbox / "uv_cwd.log"),
        "FAKE_UV_FAIL": fail,
    }
    return subprocess.run(
        ["bash", str(sandbox / "repo" / "scripts" / "run-chaos-drill.sh")],
        cwd=cwd or sandbox / "repo",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _logs(sandbox: Path) -> list[Path]:
    return sorted((sandbox / "repo" / "outputs" / "drills").glob("chaos-drill-*.log"))


def test_drill_log_is_written_under_the_repo_outputs_directory(sandbox: Path) -> None:
    result = _run(sandbox)

    assert result.returncode == 0, result.stdout + result.stderr
    logs = _logs(sandbox)
    assert len(logs) == 1
    body = logs[0].read_text()
    for playbook in PLAYBOOKS:
        assert f"--- Playbook: {playbook} ---" in body
    assert "DRILL RESULT: ALL PASS" in body
    assert f"Log saved to: {logs[0]}" in result.stdout


def test_failed_playbook_exits_nonzero_and_names_the_durable_log(sandbox: Path) -> None:
    result = _run(sandbox, fail="feed_gap")

    assert result.returncode == 1
    (log,) = _logs(sandbox)
    body = log.read_text()
    assert "Result: FAIL" in body
    assert "Passed: 4/5" in body
    assert f"review log at {log}" in body


def test_second_run_the_same_day_appends_instead_of_replacing_the_evidence(sandbox: Path) -> None:
    assert _run(sandbox, fail="disk_full").returncode == 1
    assert _run(sandbox).returncode == 0

    (log,) = _logs(sandbox)
    body = log.read_text()
    assert body.count("=== Quarterly Chaos Drill ===") == 2
    assert "DRILL RESULT: 1 FAILURES" in body  # the failed first run is still there
    assert "DRILL RESULT: ALL PASS" in body


def test_script_runs_the_playbooks_from_the_repo_root_whatever_the_caller_directory(sandbox: Path) -> None:
    elsewhere = sandbox / "elsewhere"
    elsewhere.mkdir()

    result = _run(sandbox, cwd=elsewhere)

    assert result.returncode == 0, result.stdout + result.stderr
    seen = {line for line in (sandbox / "uv_cwd.log").read_text().splitlines()}
    assert seen == {str(sandbox / "repo")}
    assert len(_logs(sandbox)) == 1  # the log is in the repo, not in the caller's directory
    assert not (elsewhere / "outputs").exists()


def test_script_and_runbook_no_longer_point_at_tmp() -> None:
    assert "/tmp/" not in SCRIPT.read_text()
    runbook = RUNBOOK.read_text()
    assert "/tmp/chaos-drill" not in runbook
    assert runbook.count("outputs/drills/chaos-drill-YYYYMMDD.log") >= 3
