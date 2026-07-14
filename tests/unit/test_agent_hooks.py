"""Behavior tests for the Claude Code hook enforcement floor (.claude/hooks/).

Hooks are exercised as subprocesses (stdin JSON -> exit code), matching how the
harness invokes them. Probe verdict 2026-07-14: subagent tool calls carry
`agent_type` in the hook input; main-session calls do not.
"""

import json
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / ".claude" / "hooks"


def run_hook(script: str, event: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=25,
    )


# --- scope_guard -------------------------------------------------------------

MARKER = {"id": "d1", "allowed": ["src/foo/*.py", "tests/unit/test_foo.py"], "orchestrator_bypass": True}


def _setup_delegation(tmp_path: Path) -> None:
    rt = tmp_path / ".agent/runtime"
    rt.mkdir(parents=True)
    (rt / "active-packet.json").write_text(json.dumps(MARKER))


def test_scope_guard_blocks_subagent_edit_outside_allowlist(tmp_path):
    _setup_delegation(tmp_path)
    ev = {
        "agent_type": "hft-executor",
        "tool_name": "Edit",
        "tool_input": {"file_path": "src/hft_platform/core/pricing.py"},
    }
    r = run_hook("scope_guard.py", ev, tmp_path)
    assert r.returncode == 2 and "scope-guard" in r.stderr


def test_scope_guard_allows_listed_path_during_delegation(tmp_path):
    _setup_delegation(tmp_path)
    ev = {"agent_type": "hft-executor", "tool_name": "Edit", "tool_input": {"file_path": "src/foo/bar.py"}}
    assert run_hook("scope_guard.py", ev, tmp_path).returncode == 0


def test_scope_guard_allows_everything_when_no_delegation_window(tmp_path):
    ev = {"agent_type": "hft-executor", "tool_name": "Write", "tool_input": {"file_path": "src/anything.py"}}
    assert run_hook("scope_guard.py", ev, tmp_path).returncode == 0


def test_scope_guard_lets_orchestrator_bypass(tmp_path):
    _setup_delegation(tmp_path)
    ev = {"tool_name": "Edit", "tool_input": {"file_path": "docs/other.md"}}  # no agent_type = main session
    assert run_hook("scope_guard.py", ev, tmp_path).returncode == 0


# --- git_guard ---------------------------------------------------------------


def test_git_guard_blocks_subagent_git_commit(tmp_path):
    ev = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": "git add -A && git commit -m x"}}
    r = run_hook("git_guard.py", ev, tmp_path)
    assert r.returncode == 2 and "git-guard" in r.stderr


def test_git_guard_allows_subagent_readonly_git(tmp_path):
    ev = {
        "agent_type": "hft-docs",
        "tool_name": "Bash",
        "tool_input": {"command": "git status --short && git log --oneline -5"},
    }
    assert run_hook("git_guard.py", ev, tmp_path).returncode == 0


def test_git_guard_ignores_main_session_git(tmp_path):
    ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m ok"}}  # no agent_type
    assert run_hook("git_guard.py", ev, tmp_path).returncode == 0


def test_git_guard_blocks_subagent_stash_drop_but_allows_stash_list(tmp_path):
    bad = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": "git stash drop"}}
    ok = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": "git stash list"}}
    assert run_hook("git_guard.py", bad, tmp_path).returncode == 2
    assert run_hook("git_guard.py", ok, tmp_path).returncode == 0


# --- discipline_feedback -----------------------------------------------------


def test_discipline_feedback_skips_non_platform_files(tmp_path):
    ev = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "docs/note.md")}}
    assert run_hook("discipline_feedback.py", ev, tmp_path).returncode == 0


def test_discipline_feedback_fails_open_when_checker_missing(tmp_path):
    # tmp cwd has no scripts/check_discipline.py -> advisory hook must not block
    ev = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "src/hft_platform/x.py")}}
    assert run_hook("discipline_feedback.py", ev, tmp_path).returncode == 0


# --- commit_audit ------------------------------------------------------------


def _git_repo_with_commit(tmp_path, files):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base"],
        cwd=tmp_path,
        check=True,
    )
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c1"],
        cwd=tmp_path,
        check=True,
    )


def test_commit_audit_warns_when_head_exceeds_allowlist(tmp_path):
    _git_repo_with_commit(tmp_path, ["a.md", "b.md"])
    rt = tmp_path / ".agent/runtime"
    rt.mkdir(parents=True)
    (rt / "commit-allowlist.json").write_text(json.dumps({"allowed": ["a.md"]}))
    ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m c1"}}
    r = run_hook("commit_audit.py", ev, tmp_path)
    assert r.returncode == 2 and "b.md" in r.stderr


def test_commit_audit_silent_without_marker(tmp_path):
    _git_repo_with_commit(tmp_path, ["a.md"])
    ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m c1"}}
    assert run_hook("commit_audit.py", ev, tmp_path).returncode == 0
