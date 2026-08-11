"""The TaskCompleted quality gate must be satisfiable, and must fail loudly.

Measured 2026-08-11: the gate rejected every completion it was ever asked to
judge. Gate 1 required ``task_output``/``result``, but TaskUpdate — the only
path by which a task is completed — carries its report in ``description``, so
"Task has no output/result" fired no matter how detailed the report was.

It failed silently as well. The rejection JSON went to stdout; an exit-2 hook
surfaces *stderr* to the model, so three closes in one session reported success
while the statuses never moved, and finished work sat at ``in_progress``.

A gate nobody can satisfy is indistinguishable from a gate nobody notices, so
these tests pin both halves: the report is read from whichever field the host
sends, and a rejection is visible.

Hooks are exercised as subprocesses (stdin JSON -> exit code), matching how the
harness invokes them and the convention in ``test_agent_hooks.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "task_completed_gate.sh"

_REPORT = "Fixed the resolution order; see src/hft_platform/services/_md_reconnect.py:384 for the decision site."


def run_gate(event: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GATE)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=25,
    )


# --------------------------------------------------------------------------- #
# Satisfiable                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_gate_accepts_a_report_carried_in_the_description() -> None:
    """The regression: TaskUpdate has no output field, only a description."""
    result = run_gate({"task_id": "15", "task_subject": "fix the log", "description": _REPORT})

    assert result.returncode == 0
    assert "passed quality gate" in result.stdout


@pytest.mark.unit
def test_gate_still_accepts_the_legacy_task_output_field() -> None:
    result = run_gate({"task_id": "15", "task_subject": "fix the log", "task_output": _REPORT})

    assert result.returncode == 0


@pytest.mark.unit
def test_gate_accepts_a_shell_or_markdown_reference() -> None:
    """Infra and docs tasks cite .sh/.md, not only .py — the extension list has
    to cover the files such a task actually touches or the gate is unsatisfiable
    for that whole class of work."""
    result = run_gate(
        {"task_id": "17", "task_subject": "fix the gate", "description": "scripts/hooks/task_completed_gate.sh:18"}
    )

    assert result.returncode == 0


# --------------------------------------------------------------------------- #
# Still a gate                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_gate_rejects_a_report_without_file_line_references() -> None:
    result = run_gate({"task_id": "15", "task_subject": "fix the log", "description": "all done, trust me"})

    assert result.returncode == 2
    assert "file:line" in result.stderr


@pytest.mark.unit
def test_gate_rejects_when_no_report_is_present_at_all() -> None:
    result = run_gate({"task_id": "15", "task_subject": "fix the log"})

    assert result.returncode == 2
    assert "no output/result" in result.stderr


@pytest.mark.unit
def test_gate_rejects_a_security_task_that_does_not_classify_severity() -> None:
    result = run_gate({"task_id": "9", "task_subject": "security scan of the order path", "description": _REPORT})

    assert result.returncode == 2
    assert "severity" in result.stderr


@pytest.mark.unit
def test_gate_survives_a_malformed_payload() -> None:
    """Fail closed on garbage rather than crashing the completion path."""
    result = subprocess.run([str(GATE)], input="not json", capture_output=True, text=True, timeout=25)

    assert result.returncode == 2


# --------------------------------------------------------------------------- #
# Loud                                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_rejection_feedback_reaches_stderr_not_only_stdout() -> None:
    """An exit-2 hook surfaces stderr to the model. Printing the rejection only
    as stdout JSON is what made three rejected closes read as successes."""
    result = run_gate({"task_id": "15", "task_subject": "fix the log", "description": "no references here"})

    assert result.returncode == 2
    assert "REJECTED by the quality gate" in result.stderr
    # The structured form stays on stdout for hosts that parse it.
    assert json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"] == "TaskCompleted"
