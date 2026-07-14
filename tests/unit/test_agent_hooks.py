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


def test_git_guard_ignores_git_mentions_inside_quoted_arguments(tmp_path):
    # Reviewer F1: grep/echo text mentioning git must not be parsed as git commands
    for cmd in (
        'grep -rn "git push" docs/',
        'echo "this is a non-git command about git execution"',
        "rg 'git commit' .agent/rules/30-git.md",
    ):
        ev = {"agent_type": "hft-reviewer", "tool_name": "Bash", "tool_input": {"command": cmd}}
        r = run_hook("git_guard.py", ev, tmp_path)
        assert r.returncode == 0, f"falsely blocked: {cmd!r} -> {r.stderr}"


def test_git_guard_allows_readonly_git_with_global_options(tmp_path):
    # Reviewer F3: git -C <path> / git -c k=v read-only forms are permitted
    for cmd in (
        "git -C /home/charlie/hft_platform show 1e8619d1 --stat",
        "git -c core.pager=cat log -1",
    ):
        ev = {"agent_type": "hft-reviewer", "tool_name": "Bash", "tool_input": {"command": cmd}}
        r = run_hook("git_guard.py", ev, tmp_path)
        assert r.returncode == 0, f"falsely blocked: {cmd!r} -> {r.stderr}"


def test_git_guard_blocks_mutation_behind_global_options_and_chains(tmp_path):
    for cmd in (
        "git -C /repo commit -m x",
        "grep ok file.txt && git push origin main",
    ):
        ev = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": cmd}}
        assert run_hook("git_guard.py", ev, tmp_path).returncode == 2, f"not blocked: {cmd!r}"


def test_scope_guard_blocks_subagent_rewriting_runtime_markers(tmp_path):
    # Reviewer F2: the guarded party must not be able to rewrite the guard's marker
    _setup_delegation(tmp_path)
    for target in (".agent/runtime/active-packet.json", ".agent/runtime/commit-allowlist.json"):
        ev = {"agent_type": "hft-executor", "tool_name": "Write", "tool_input": {"file_path": target}}
        r = run_hook("scope_guard.py", ev, tmp_path)
        assert r.returncode == 2, f"marker rewrite allowed: {target}"


def test_scope_guard_blocks_scratchpad_traversal_escape(tmp_path):
    # Reviewer F4: /tmp/claude-.../../../ escape must not bypass the window
    _setup_delegation(tmp_path)
    ev = {
        "agent_type": "hft-executor",
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/claude-1000/../../home/x/repo/src/hft_platform/core/pricing.py"},
    }
    assert run_hook("scope_guard.py", ev, tmp_path).returncode == 2


def test_scope_guard_allows_real_scratchpad_paths(tmp_path):
    _setup_delegation(tmp_path)
    ev = {
        "agent_type": "hft-executor",
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/claude-1000/session/scratchpad/notes.md"},
    }
    assert run_hook("scope_guard.py", ev, tmp_path).returncode == 0


def test_scope_guard_exact_file_pattern_does_not_grant_prefix_siblings(tmp_path):
    # Reviewer F6: allowlisted tests/unit/test_foo.py must not grant test_foo.py.orig
    _setup_delegation(tmp_path)
    for target in ("tests/unit/test_foo.py.orig", "src/foobar_other.py"):
        ev = {"agent_type": "hft-executor", "tool_name": "Write", "tool_input": {"file_path": target}}
        r = run_hook("scope_guard.py", ev, tmp_path)
        assert r.returncode == 2, f"prefix sibling allowed: {target}"


def test_scope_guard_blocks_absolute_path_outside_allowlist(tmp_path):
    # Reviewer F8: the harness sends ABSOLUTE paths; relpath branch must still block
    _setup_delegation(tmp_path)
    ev = {
        "agent_type": "hft-executor",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "docs/other.md")},
    }
    assert run_hook("scope_guard.py", ev, tmp_path).returncode == 2


def test_scope_guard_allows_absolute_path_inside_allowlist(tmp_path):
    _setup_delegation(tmp_path)
    ev = {
        "agent_type": "hft-executor",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "src/foo/bar.py")},
    }
    assert run_hook("scope_guard.py", ev, tmp_path).returncode == 0


# --- discipline_feedback -----------------------------------------------------


def test_discipline_feedback_skips_non_platform_files(tmp_path):
    ev = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "docs/note.md")}}
    assert run_hook("discipline_feedback.py", ev, tmp_path).returncode == 0


def test_discipline_feedback_fails_open_when_checker_missing(tmp_path):
    # tmp cwd has no scripts/check_discipline.py -> advisory hook must not block
    ev = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "src/hft_platform/x.py")}}
    assert run_hook("discipline_feedback.py", ev, tmp_path).returncode == 0


def test_discipline_feedback_relays_checker_findings(tmp_path):
    # Reviewer F8: positive path — a failing checker must surface as exit-2 feedback
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check_discipline.py").write_text(
        "import sys\nprint('CRITICAL: datetime.now in hot path')\nsys.exit(1)\n"
    )
    ev = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "src/hft_platform/x.py")}}
    r = run_hook("discipline_feedback.py", ev, tmp_path)
    assert r.returncode == 2 and "datetime.now" in r.stderr


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


def test_commit_audit_silent_when_head_matches_allowlist(tmp_path):
    # Reviewer F8: the within-allowlist quiet path
    _git_repo_with_commit(tmp_path, ["a.md", "b.md"])
    rt = tmp_path / ".agent/runtime"
    rt.mkdir(parents=True)
    (rt / "commit-allowlist.json").write_text(json.dumps({"allowed": ["a.md", "b.md"]}))
    ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m c1"}}
    assert run_hook("commit_audit.py", ev, tmp_path).returncode == 0
