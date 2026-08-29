"""Behavior tests for the Claude Code hook enforcement floor (.claude/hooks/).

Hooks are exercised as subprocesses (stdin JSON -> exit code), matching how the
harness invokes them. Probe verdict 2026-07-14: subagent tool calls carry
`agent_type` in the hook input; main-session calls do not.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / ".claude" / "hooks"


def run_hook(script: str, event: dict, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=25,
        env={**os.environ, **env} if env else None,
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


# --- codex_review_gate -------------------------------------------------------
#
# The gate this exercises replaced one whose credit rested on a stamp written at
# review *launch*. Measured over the 25 report directories that existed on
# 2026-08-29: the native reviewer had died in 9 of them, 24 carried a
# `needs-attention` verdict, and every single one opened the gate.

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]


def _run_git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _gated_repo(tmp_path, files=("src/hft_platform/x.py",), seeded=()):
    """A repo with origin/main behind a commit that touches `files`.

    `seeded` files exist already at origin/main, so a branch that MOVES one of
    them is a deletion from a gated prefix rather than an unrelated addition.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    for f in seeded:
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("seed = 0\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = _run_git(repo, "rev-parse", "HEAD")
    _run_git(repo, "update-ref", "refs/remotes/origin/main", base)
    for f in files:
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("value = 1\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "work")
    return repo, _run_git(repo, "rev-parse", "HEAD")


def _canonical_sha(repo, head, base=None):
    """The documented hash formula, recomputed independently of the hook."""
    import hashlib

    base = base or _run_git(repo, "merge-base", "origin/main", head)
    raw = subprocess.run(
        ["git", "diff", "--no-renames", "--full-index", f"{base}..{head}"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.sha256(raw).hexdigest()


def _report(
    reports,
    repo,
    head,
    *,
    name="20260829-120000-t",
    mode="branch",
    native_ok=True,
    verdict="approve",
    diff_sha=None,
    adversarial_body="",
    native_body="",
    ack=None,
    complete=True,
    base=None,
):
    d = reports / name
    d.mkdir(parents=True)
    (d / "review.md").write_text(
        "# Codex Review\n\nTarget: branch diff against origin/main\n\n"
        + (native_body or "It looks fine.\n" if native_ok else "Reviewer failed to output a response.\n")
    )
    (d / "adversarial.md").write_text(
        f"# Codex Adversarial Review\n\nTarget: branch diff\nVerdict: {verdict}\n\n{adversarial_body}"
    )
    if ack is not None:
        (d / "ACK.md").write_text(ack)
    (d / "ATTESTATION.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "mode": mode,
                "base_oid": base or _run_git(repo, "merge-base", "origin/main", head),
                "head_oid": head,
                "pr_number": None,
                "diff_sha256": diff_sha if diff_sha is not None else _canonical_sha(repo, head, base),
                "reviewers": {
                    "review": {"completed": native_ok, "failure": None},
                    "adversarial": {"completed": True, "verdict": verdict},
                },
                "complete": complete,
            }
        )
    )
    return d


def _push(repo, reports, command="git push origin main"):
    return run_hook(
        "codex_review_gate.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": command}},
        repo,
        env={"DUAL_REVIEW_REPORTS": str(reports)},
    )


def test_review_gate_allows_a_push_with_a_complete_approving_review(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    assert _push(repo, tmp_path / "reports").returncode == 0


def test_review_gate_blocks_when_the_native_reviewer_failed(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head, native_ok=False, complete=False)
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2
    assert "never finished" in r.stderr or "produced no report" in r.stderr


def test_review_gate_blocks_needs_attention_without_an_ack(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(
        tmp_path / "reports",
        repo,
        head,
        verdict="needs-attention",
        adversarial_body="Findings:\n- [high] Latch is released on restart (src/a.py:10-12)\n",
    )
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "ACK.md" in r.stderr


def test_review_gate_allows_needs_attention_with_a_reasoned_ack(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(
        tmp_path / "reports",
        repo,
        head,
        verdict="needs-attention",
        adversarial_body="Findings:\n- [high] Latch is released on restart (src/a.py:10-12)\n",
        ack="- [high] Latch is released on restart\n  Accepted: the restart path is unreachable on this branch.\n",
    )
    assert _push(repo, tmp_path / "reports").returncode == 0


def test_review_gate_blocks_an_ack_that_omits_a_high_finding(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(
        tmp_path / "reports",
        repo,
        head,
        verdict="needs-attention",
        adversarial_body=(
            "Findings:\n"
            "- [high] Latch is released on restart (src/a.py:10-12)\n"
            "- [high] Token is never registered (src/b.py:5-6)\n"
        ),
        ack="- [high] Latch is released on restart\n  Accepted: unreachable on this branch, see the guard.\n",
    )
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "Token is never registered" in r.stderr


def test_review_gate_blocks_an_ack_that_names_a_finding_without_a_reason(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(
        tmp_path / "reports",
        repo,
        head,
        verdict="needs-attention",
        adversarial_body="Findings:\n- [high] Latch is released on restart (src/a.py:10-12)\n",
        ack="- [high] Latch is released on restart\n  ok\n",
    )
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "no reason" in r.stderr


def test_review_gate_blocks_when_the_attestation_covers_a_different_diff(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head, diff_sha="0" * 64)
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "different diff" in r.stderr


def test_review_gate_blocks_after_the_reviewed_commit_is_amended(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    (repo / "src/hft_platform/x.py").write_text("value = 2\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "--amend", "-m", "work amended")
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "no completed Codex review" in r.stderr


def test_review_gate_refuses_a_working_tree_attestation_for_a_push(tmp_path):
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head, mode="working-tree")
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "working tree" in r.stderr


def test_review_gate_detects_a_push_behind_git_global_options(tmp_path):
    repo, head = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    for cmd in (f"git -C {repo} push origin main", "git -c push.default=current push"):
        r = _push(repo, reports, command=cmd)
        assert r.returncode == 2, f"{cmd!r} was not gated"


def test_review_gate_detects_gh_pr_create_behind_global_options(tmp_path):
    repo, _ = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    r = _push(repo, reports, command="gh --repo owner/name pr create --fill")
    assert r.returncode == 2


def test_review_gate_gates_a_source_file_moved_out_of_src(tmp_path):
    """A move deletes production source; rename detection would hide the deletion.

    `git diff --name-only` with rename detection reports ONLY the destination, so
    src/hft_platform/seeded.py -> docs/seeded.py shows the gate no gated prefix.
    `--no-renames` reports both sides. Verified against 22a94ae0 in this repo.
    """
    repo, _ = _gated_repo(tmp_path, files=("docs/other.md",), seeded=("src/hft_platform/seeded.py",))
    reports = tmp_path / "reports"
    reports.mkdir()
    (repo / "docs").mkdir(exist_ok=True)
    _run_git(repo, "mv", "src/hft_platform/seeded.py", "docs/seeded.py")
    _run_git(repo, "commit", "-q", "-m", "move source out of src")

    with_renames = _run_git(repo, "diff", "--name-only", "-M", "origin/main...HEAD")
    assert not any(line.startswith("src/") for line in with_renames.splitlines()), (
        "premise broken: rename detection was expected to hide the src/ path"
    )

    r = _push(repo, reports)
    assert r.returncode == 2, "a rename out of src/ escaped the gate"


def test_review_gate_gates_config_changes(tmp_path):
    repo, _ = _gated_repo(tmp_path, files=("config/live/strategies.yaml",))
    reports = tmp_path / "reports"
    reports.mkdir()
    assert _push(repo, reports).returncode == 2


def test_review_gate_allows_a_docs_only_push(tmp_path):
    repo, _ = _gated_repo(tmp_path, files=("docs/notes.md",))
    reports = tmp_path / "reports"
    reports.mkdir()
    assert _push(repo, reports).returncode == 0


def test_review_gate_allows_a_ref_deletion(tmp_path):
    repo, _ = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    assert _push(repo, reports, command="git push origin --delete stale").returncode == 0


def test_review_gate_blocks_an_unresolvable_refspec(tmp_path):
    repo, _ = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    r = _push(repo, reports, command="git push origin no-such-branch")
    assert r.returncode == 2 and "cannot resolve" in r.stderr


def test_review_gate_honours_an_explicit_override(tmp_path):
    repo, _ = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    r = _push(repo, reports, command="CODEX_REVIEW_OVERRIDE=1 git push origin main")
    assert r.returncode == 0 and "overridden" in r.stderr


def test_review_gate_ignores_a_push_mentioned_inside_a_quoted_argument(tmp_path):
    repo, _ = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    assert _push(repo, reports, command='echo "git push origin main"').returncode == 0


# --- attestation producer ----------------------------------------------------
#
# `dual-review.sh` and its waiter live in ~/.claude/bin, outside this repository
# and outside its CI. The rules they apply live here so they are tested; these
# cover the two reviewers' completion contracts, which differ because the two
# commands differ: /codex:review renders free-form prose, /codex:adversarial-review
# renders against schemas/review-output.schema.json.


def _attestation_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("review_attestation", HOOKS / "review_attestation.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules[cls.__module__], so the
    # module must be registered before it executes or @dataclass raises.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


NATIVE_DEAD = "# Codex Review\n\nTarget: branch diff\n\nReviewer failed to output a response.\n"
NATIVE_OK = "# Codex Review\n\nTarget: branch diff\n\n- [P1] Move the write off the loop — src/a.py:1\n"
ADV_LIMIT = "# Codex Adversarial Review\n\nCodex did not return valid structured JSON.\n\n- Parse error: usage limit\n"
ADV_OK = "# Codex Adversarial Review\n\nVerdict: needs-attention\n\nFindings:\n- [high] Latch released (src/a.py:1-2)\n- [low] Typo (src/b.py:9)\n"


def test_attestation_marks_a_dead_native_reviewer_incomplete(tmp_path):
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_DEAD,
        adversarial=ADV_OK,
        base=None,
        head="x",
        now="t",
    )
    assert a["reviewers"]["review"]["completed"] is False
    assert a["reviewers"]["review"]["failure"] == "Reviewer failed to output a response."
    assert a["complete"] is False


def test_attestation_marks_a_usage_limited_adversarial_reviewer_incomplete(tmp_path):
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_OK,
        adversarial=ADV_LIMIT,
        base=None,
        head="x",
        now="t",
    )
    assert a["reviewers"]["adversarial"] == {"completed": False, "verdict": None}
    assert a["complete"] is False


def test_attestation_reads_the_verdict_from_the_adversarial_report(tmp_path):
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_OK,
        adversarial=ADV_OK,
        base=None,
        head="x",
        now="t",
    )
    assert a["reviewers"]["adversarial"]["verdict"] == "needs-attention"


def test_attestation_never_completes_for_a_working_tree_review(tmp_path):
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="working-tree",
        pr_number=None,
        native=NATIVE_OK,
        adversarial=ADV_OK.replace("needs-attention", "approve"),
        base=None,
        head="x",
        now="t",
    )
    assert a["complete"] is False and a["diff_sha256"] is None


def test_attestation_collects_findings_from_both_reviewers():
    m = _attestation_module()
    found = m.all_findings(NATIVE_OK, ADV_OK)
    assert {"adversarial", "review"} == {f["reviewer"] for f in found}
    assert ("high", "Latch released") in [(f["severity"], f["title"]) for f in found]


def test_only_high_severity_findings_require_an_ack(tmp_path):
    m = _attestation_module()
    d = tmp_path / "r"
    d.mkdir()
    (d / "adversarial.md").write_text(ADV_OK)
    (d / "review.md").write_text(NATIVE_OK)
    required = m.findings_needing_ack(d)
    assert ("high", "Latch released") in required
    assert ("P1", "Move the write off the loop") in required
    assert not any(t == "Typo" for _, t in required), "a low finding must not demand an ACK"


def _stub_gh(tmp_path, head_oid):
    """A `gh` on PATH that answers `pr view --json headRefOid` and nothing else."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nprintf \'{{"headRefOid":"{head_oid}"}}\'\n')
    gh.chmod(0o755)
    return bin_dir


def test_review_gate_blocks_a_pr_head_that_is_not_present_locally(tmp_path):
    """A PR head that was never fetched must not read as "nothing to gate".

    `canonical_range` returns None both when origin/main is missing (not this
    project -- allow) and when the head object is absent (cannot inspect it --
    block). Conflating the two would let `gh pr merge` publish unreviewed code.
    """
    repo, _ = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    absent = "0" * 39 + "1"
    r = run_hook(
        "codex_review_gate.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "gh pr merge 460 --squash"}},
        repo,
        env={
            "DUAL_REVIEW_REPORTS": str(reports),
            "PATH": f"{_stub_gh(tmp_path, absent)}{os.pathsep}{os.environ['PATH']}",
        },
    )
    assert r.returncode == 2 and "not present locally" in r.stderr


def test_review_gate_allows_a_push_in_a_repository_without_origin_main(tmp_path):
    """The other half of that fork: an unrelated repo must not be gated at all."""
    repo = tmp_path / "other"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src/x.py").write_text("v = 1\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "work")
    reports = tmp_path / "reports"
    reports.mkdir()
    assert _push(repo, reports).returncode == 0


def test_verifier_reports_a_missing_attestation_rather_than_crashing(tmp_path):
    m = _attestation_module()
    m.REPORTS = tmp_path / "nope"
    d = m.verify(str(tmp_path), "a" * 40)
    assert d.ok is False and "review reports directory" in d.reason


def test_review_gate_survives_origin_main_advancing_after_the_review(tmp_path):
    """A benchmark-baseline commit on main must not invalidate a real review.

    The hash is bound to the base the reviewers actually used, so main moving
    underneath does not silently re-derive a different range.
    """
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)

    # main advances with an unrelated commit, exactly as CI does on every run
    main_tip = _run_git(repo, "rev-parse", "origin/main")
    _run_git(repo, "branch", "-f", "tmp-main", main_tip)
    _run_git(repo, "checkout", "-q", "tmp-main")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs/changelog.md").write_text("advanced\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "unrelated main commit")
    _run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _run_git(repo, "checkout", "-q", head)

    assert _push(repo, tmp_path / "reports", command=f"git push origin {head}").returncode == 0


def test_review_gate_blocks_a_review_whose_base_was_narrowed(tmp_path):
    """`--base HEAD^` reviews one commit and would otherwise stamp the branch."""
    repo, _ = _gated_repo(tmp_path, files=("src/hft_platform/first.py",))
    second = repo / "src/hft_platform/second.py"
    second.write_text("v = 2\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "second source commit")
    head = _run_git(repo, "rev-parse", "HEAD")
    narrow_base = _run_git(repo, "rev-parse", "HEAD~1")

    _report(tmp_path / "reports", repo, head, base=narrow_base)
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2, "a narrowed review base credited unreviewed files"
    assert "NARROWER" in r.stderr


def test_review_gate_blocks_an_attestation_with_no_recorded_base(tmp_path):
    repo, head = _gated_repo(tmp_path)
    d = _report(tmp_path / "reports", repo, head)
    att = json.loads((d / "ATTESTATION.json").read_text())
    att["base_oid"] = None
    (d / "ATTESTATION.json").write_text(json.dumps(att))
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "no reviewed base" in r.stderr


# --- regressions for the 2026-08-29 dual review ------------------------------
#
# Every case below was reported by one or both Codex reviewers against the first
# three commits of this branch, and reproduced before being fixed.


def test_review_gate_blocks_a_narrowed_review_that_reuses_the_same_file(tmp_path):
    """Comparing gated FILENAMES is not enough when both commits touch one file.

    The reviewers reproduced this: an unreviewed commit and a reviewed commit
    both modify src/hft_platform/risk.py, so the two filename sets are equal and
    a set-difference check sees nothing missing. Ancestry is the exact test.
    """
    repo, _ = _gated_repo(tmp_path, files=("src/hft_platform/risk.py",))
    (repo / "src/hft_platform/risk.py").write_text("limit = 5000\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "second change to the SAME file")
    head = _run_git(repo, "rev-parse", "HEAD")
    narrow_base = _run_git(repo, "rev-parse", "HEAD~1")

    reviewed = set(_run_git(repo, "diff", "--name-only", "--no-renames", f"{narrow_base}..{head}").split())
    publishing = set(_run_git(repo, "diff", "--name-only", "--no-renames", f"origin/main..{head}").split())
    assert publishing == reviewed, "premise broken: the filename sets were expected to be equal"

    _report(tmp_path / "reports", repo, head, base=narrow_base)
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "NARROWER" in r.stderr


def test_review_gate_requires_an_ack_for_a_native_p1_even_when_adversarial_approves(tmp_path):
    repo, head = _gated_repo(tmp_path)
    d = _report(tmp_path / "reports", repo, head, verdict="approve")
    att = json.loads((d / "ATTESTATION.json").read_text())
    att["findings"] = [{"reviewer": "review", "severity": "P1", "title": "Blocking native finding"}]
    (d / "ATTESTATION.json").write_text(json.dumps(att))
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2 and "Blocking native finding" in r.stderr


def test_review_gate_keeps_blocking_findings_when_the_reports_are_deleted(tmp_path):
    """Findings live in the write-once attestation, not in mutable markdown."""
    repo, head = _gated_repo(tmp_path)
    d = _report(tmp_path / "reports", repo, head, verdict="needs-attention")
    att = json.loads((d / "ATTESTATION.json").read_text())
    att["findings"] = [{"reviewer": "adversarial", "severity": "high", "title": "A real problem"}]
    (d / "ATTESTATION.json").write_text(json.dumps(att))
    (d / "adversarial.md").unlink()
    (d / "review.md").unlink()
    r = _push(repo, tmp_path / "reports")
    assert r.returncode == 2, "deleting the reports turned NO SHIP into an approval"
    assert "A real problem" in r.stderr


def test_review_gate_fails_closed_on_a_multi_ref_push(tmp_path):
    """`git push --all` publishes refs the gate cannot enumerate."""
    repo, _ = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    for cmd in ("git push --all origin", "git push --mirror origin", "git push --branches origin"):
        r = _push(repo, reports, command=cmd)
        assert r.returncode == 2, f"{cmd!r} published unenumerated refs"
        assert "cannot enumerate" in r.stderr or "cannot resolve" in r.stderr


def test_review_gate_does_not_mistake_a_push_option_value_for_a_refspec(tmp_path):
    """`git push -o ci.skip` must gate HEAD, not try to resolve 'ci.skip'."""
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    r = _push(repo, tmp_path / "reports", command="git push -o ci.skip origin main")
    assert r.returncode == 0, r.stderr


def test_review_gate_resolves_the_worktree_named_by_git_dash_c(tmp_path):
    """`git -C <other> push` publishes THAT worktree's HEAD, not this one's."""
    other, other_head = _gated_repo(tmp_path)
    here = tmp_path / "here"
    here.mkdir()
    _run_git(here, "init", "-q", "-b", "main")
    _run_git(here, "commit", "-q", "--allow-empty", "-m", "docs only")
    _run_git(here, "update-ref", "refs/remotes/origin/main", "HEAD")
    reports = tmp_path / "reports"
    reports.mkdir()
    r = run_hook(
        "codex_review_gate.py",
        {"tool_name": "Bash", "cwd": str(here), "tool_input": {"command": f"git -C {other} push origin main"}},
        here,
        env={"DUAL_REVIEW_REPORTS": str(reports)},
    )
    assert r.returncode == 2, "the other worktree's gated changes were attested against this HEAD"
    assert other_head[:8] in r.stderr or "no completed Codex review" in r.stderr


def test_review_gate_sees_a_push_behind_an_env_assignment_wrapper(tmp_path):
    """`env LANG=C git push` was invisible to the parser entirely."""
    repo, _ = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    assert _push(repo, reports, command="env LANG=C git push origin main").returncode == 2


def test_git_guard_blocks_a_subagent_mutation_behind_an_env_wrapper(tmp_path):
    """The same parser gap let a subagent amend history unseen."""
    _git_repo_with_commit(tmp_path, ["a.md"])
    ev = {
        "tool_name": "Bash",
        "agent_type": "hft-executor",
        "tool_input": {"command": "env GIT_EDITOR=true git commit --amend"},
    }
    r = run_hook("git_guard.py", ev, tmp_path)
    assert r.returncode == 2 and "git commit" in r.stderr


def test_review_gate_gates_changes_to_its_own_enforcement_code(tmp_path):
    """A change that weakens the gate must not publish unreviewed."""
    repo, _ = _gated_repo(tmp_path, files=(".claude/hooks/codex_review_gate.py",))
    reports = tmp_path / "reports"
    reports.mkdir()
    assert _push(repo, reports).returncode == 2
