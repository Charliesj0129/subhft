"""Contract tests for the unattended-routine runner (v3 W3, ADR 002).

The runner is the ONLY entry point for headless routines: read-only tool set
enforced via --disallowedTools, and any routine whose write_scope is not
`none` is refused outright.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/agent_routines/run_routine.sh"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=15)


def test_dry_run_prints_claude_command_without_executing():
    r = _run("R1-nightly-ci-triage", "--dry-run")
    assert r.returncode == 0
    assert "claude -p" in r.stdout and "disallowedTools" in r.stdout


def test_unknown_routine_fails_loudly():
    r = _run("R9-nonexistent", "--dry-run")
    assert r.returncode != 0 and "unknown routine" in r.stderr


def test_write_scope_other_than_none_is_refused(tmp_path, monkeypatch):
    rogue = REPO / ".agent/routines/R9-test-rogue-writer.md"
    rogue.write_text("---\nname: R9-test-rogue-writer\nwrite_scope: docs/\nnotify: file\n---\nbody\n")
    try:
        r = _run("R9-test-rogue-writer", "--dry-run")
        assert r.returncode != 0 and "write_scope" in r.stderr
    finally:
        rogue.unlink()
