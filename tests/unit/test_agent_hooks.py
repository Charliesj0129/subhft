"""Behavior tests for the Claude Code hook enforcement floor (.claude/hooks/).

Hooks are exercised as subprocesses (stdin JSON -> exit code), matching how the
harness invokes them. Probe verdict 2026-07-14: subagent tool calls carry
`agent_type` in the hook input; main-session calls do not.
"""

import json
import os
import shutil
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


def test_review_gate_blocks_an_attestation_marked_incomplete(tmp_path):
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


def test_attestation_records_a_dead_native_reviewer_without_blocking_on_it(tmp_path):
    """The native reviewer is advisory: recorded as failed, but not a veto.

    Built against a REAL repo so the diff hash is genuine. The earlier version
    of this test passed ``base=None``, which made ``complete`` false because no
    diff could be hashed -- it would have gone on passing whichever way the
    native reviewer was treated, and said nothing about the rule in its name.
    """
    _git_repo_with_commit(tmp_path, ["src/a.py"])
    base = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_DEAD,
        adversarial=ADV_OK,
        adversarial_status="0",
        base=base,
        head=head,
        now="t",
    )
    assert a["reviewers"]["review"]["completed"] is False
    assert a["reviewers"]["review"]["failure"] == "Reviewer failed to output a response."
    assert a["diff_sha256"], "the diff must actually hash, or this proves nothing"
    assert a["complete"] is True


def test_attestation_still_blocks_when_the_adversarial_reviewer_died(tmp_path):
    """The one reviewer that does gate. Same real-repo construction as above."""
    _git_repo_with_commit(tmp_path, ["src/a.py"])
    base = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_OK,
        adversarial="# Codex Adversarial Review\n\nCodex review failed.\n",
        base=base,
        head=head,
        now="t",
    )
    assert a["diff_sha256"], "the diff must actually hash, or this proves nothing"
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
    assert a["reviewers"]["adversarial"]["completed"] is False
    assert a["reviewers"]["adversarial"]["verdict"] is None
    assert a["complete"] is False


# The provider's own abort line, verbatim from
# 20260829-144619-review-gate-r2/adversarial.err. The URLs are trimmed; the
# prefix is the part the verifier matches on.
ERR_ABORTED = (
    "[codex] Command completed: /bin/bash -lc 'git rev-parse --show-toplevel' (exit 0)\n"
    "[codex] Codex error: You've hit your usage limit. Upgrade to Pro, visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 6:03 PM.\n"
    "[codex] Turn failed.\n"
)
ERR_CLEAN = "[codex] Review output captured.\n[codex] Reviewer finished.\n[codex] Turn completed.\n"
# What a reviewer aborted mid-investigation actually renders: the schema
# validated, so a verdict exists, over a body that reached no findings.
ADV_TRUNCATED_APPROVE = (
    "# Codex Adversarial Review\n\nTarget: branch diff against origin/main\nVerdict: approve\n\n"
    "I\u2019m applying the repository\u2019s task-intake and strict-review procedures.\n\nNo material findings.\n"
)


def test_attestation_refuses_an_approve_the_provider_aborted_mid_run(tmp_path):
    """A verdict proves the schema parsed, not that the reviewer finished looking.

    Measured 2026-08-29 on this branch's own round-2 review: the usage limit hit,
    `review.md` came back the 95-byte dead body, and the adversarial reviewer
    rendered `Verdict: approve` / "No material findings" from 375 bytes. Crediting
    that is crediting an empty review as an approval -- the failure the gate
    exists to close, arriving through the gate's own evidence channel.
    """
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_OK,
        adversarial=ADV_TRUNCATED_APPROVE,
        adversarial_err=ERR_ABORTED,
        base=None,
        head="x",
        now="t",
    )
    assert a["reviewers"]["adversarial"]["completed"] is False
    assert a["reviewers"]["adversarial"]["verdict"] == "approve"
    assert "usage limit" in a["reviewers"]["adversarial"]["failure"]
    assert a["complete"] is False


def test_attestation_refuses_a_native_report_the_provider_aborted_mid_run(tmp_path):
    """The four failure bodies are not the only way the native reviewer dies.

    A run aborted after it had already written prose leaves a report that passes
    the body check, so the sidecar is the second lock.
    """
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_OK,
        native_err=ERR_ABORTED,
        adversarial=ADV_OK,
        base=None,
        head="x",
        now="t",
    )
    assert a["reviewers"]["review"]["completed"] is False
    assert "usage limit" in a["reviewers"]["review"]["failure"]
    assert a["complete"] is False


def test_attestation_accepts_a_clean_sidecar(tmp_path):
    """The premise of the two tests above: without the abort line, both complete."""
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_OK,
        native_err=ERR_CLEAN,
        adversarial=ADV_TRUNCATED_APPROVE,
        adversarial_err=ERR_CLEAN,
        adversarial_status="0",
        base=None,
        head="x",
        now="t",
    )
    assert a["reviewers"]["review"]["completed"] is True
    assert a["reviewers"]["adversarial"]["completed"] is True


def test_attestation_does_not_treat_turn_failed_alone_as_an_abort(tmp_path):
    """`Turn failed.` adds nothing PROVIDER_ABORT does not already say.

    It is coextensive with the provider's error line across all 28 historical
    reports, so treating it as authoritative on its own would only add a way to
    be wrong about a run that recovered.
    """
    m = _attestation_module()
    a = m.build_attestation(
        repo_root=str(tmp_path),
        mode="branch",
        pr_number=None,
        native=NATIVE_OK,
        native_err="[codex] Turn failed.\n",
        adversarial=ADV_OK,
        adversarial_err="[codex] Turn failed.\n",
        adversarial_status="0",
        base=None,
        head="x",
        now="t",
    )
    assert a["reviewers"]["review"]["completed"] is True
    assert a["reviewers"]["adversarial"]["completed"] is True


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
        {
            "tool_name": "Bash",
            "cwd": str(repo),
            # --match-head-commit is now mandatory for a merge, so it is pinned
            # here to the same absent OID: this test is about a head that cannot
            # be inspected, not about the pin being missing.
            "tool_input": {"command": f"gh pr merge 460 --squash --match-head-commit {absent}"},
        },
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


# --- regressions for the 2026-08-29 adversarial review ------------------------


def test_git_guard_blocks_a_mutation_behind_a_valued_wrapper_option(tmp_path):
    """`env -u NAME git commit` parsed to no invocation at all, so it was allowed.

    The option value is the token that does NOT look like a flag; skipping only
    dash-prefixed tokens stopped the walk on it and reported "no git here". The
    worst reachable form was `timeout -s TERM 5 git reset --hard HEAD^`, which
    destroys uncommitted user work.
    """
    for cmd in (
        "env -u GIT_CONFIG git commit --amend",
        "timeout -s TERM 5 git reset --hard HEAD^",
        "env -i -u X git push",
        "nice --adjustment 10 git push",
    ):
        r = run_hook(
            "git_guard.py",
            {"tool_name": "Bash", "agent_type": "hft-docs", "tool_input": {"command": cmd}},
            tmp_path,
        )
        assert r.returncode == 2, f"{cmd!r} was allowed"


def test_git_guard_still_allows_a_read_only_command_behind_a_wrapper(tmp_path):
    """The fail-closed rule must not swallow the read-only forms subagents need."""
    r = run_hook(
        "git_guard.py",
        {"tool_name": "Bash", "agent_type": "hft-docs", "tool_input": {"command": "timeout -s TERM 5 git status"}},
        tmp_path,
    )
    assert r.returncode == 0


def test_review_gate_blocks_a_merge_of_a_different_repository(tmp_path):
    """`gh --repo other/project pr merge` resolved the selector in THIS repo.

    A reviewed PR here would then authorize a server-side merge of unrelated,
    unreviewed code there. The diff hash can only be recomputed against a local
    worktree, so a foreign --repo can never be verified -- it must block.
    """
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    r = run_hook(
        "codex_review_gate.py",
        {
            "tool_name": "Bash",
            "cwd": str(repo),
            "tool_input": {"command": f"gh --repo other/project pr merge 460 --match-head-commit {head}"},
        },
        repo,
        env={
            "DUAL_REVIEW_REPORTS": str(tmp_path / "reports"),
            "PATH": f"{_stub_gh(tmp_path, head)}{os.pathsep}{os.environ['PATH']}",
        },
    )
    assert r.returncode == 2 and "not this repository" in r.stderr


def test_review_gate_blocks_a_merge_without_a_pinned_head(tmp_path):
    """The head GitHub merges can advance after the gate reads it.

    Verifying `headRefOid` and then allowing an unpinned `gh pr merge` leaves a
    window in which a bot or a concurrent actor moves the branch, so the OID
    that was reviewed is not the OID that lands.
    """
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    r = run_hook(
        "codex_review_gate.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "gh pr merge 460 --squash"}},
        repo,
        env={
            "DUAL_REVIEW_REPORTS": str(tmp_path / "reports"),
            "PATH": f"{_stub_gh(tmp_path, head)}{os.pathsep}{os.environ['PATH']}",
        },
    )
    assert r.returncode == 2 and "--match-head-commit" in r.stderr


def test_review_gate_blocks_a_pin_that_disagrees_with_the_live_pr_head(tmp_path):
    """A pin is only evidence when it matches what GitHub reports right now."""
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    stale = "0" * 39 + "1"
    r = run_hook(
        "codex_review_gate.py",
        {
            "tool_name": "Bash",
            "cwd": str(repo),
            "tool_input": {"command": f"gh pr merge 460 --match-head-commit {stale}"},
        },
        repo,
        env={
            "DUAL_REVIEW_REPORTS": str(tmp_path / "reports"),
            "PATH": f"{_stub_gh(tmp_path, head)}{os.pathsep}{os.environ['PATH']}",
        },
    )
    assert r.returncode == 2 and "but PR head is" in r.stderr


def test_review_gate_allows_a_command_that_publishes_nothing(tmp_path):
    """Ported from the superseded tests/unit/test_codex_review_gate.py."""
    repo, _ = _gated_repo(tmp_path)
    r = run_hook(
        "codex_review_gate.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "git status"}},
        repo,
        env={"DUAL_REVIEW_REPORTS": str(tmp_path / "reports")},
    )
    assert r.returncode == 0


def test_review_gate_allows_rather_than_wedging_on_malformed_stdin(tmp_path):
    """Ported from the superseded module. A gate that wedges every command when
    its own input is unreadable costs more than it protects."""
    r = run_hook("codex_review_gate.py", "not json at all", tmp_path)
    assert r.returncode == 0


# --- Adversarial review r5: five paths that published or mutated unseen -------


def test_git_guard_blocks_a_mutation_hidden_in_a_shell_c_payload(tmp_path):
    """`bash -c 'git reset --hard HEAD^'` reached neither guard.

    After shlex the payload is ONE token, so the basename of every token is
    "bash", "-c", or the whole command string -- and both parsers concluded the
    segment does not run git. A direct probe had git_guard exiting 0 on a
    destructive reset issued by a subagent.
    """
    for cmd in (
        "bash -c 'git reset --hard HEAD^'",
        'sh -c "git push origin HEAD"',
        "bash -lc 'git commit -m x'",
        "bash -c 'bash -c \"git reset --hard\"'",
    ):
        ev = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": cmd}}
        r = run_hook("git_guard.py", ev, tmp_path)
        assert r.returncode == 2, f"not blocked: {cmd}\n{r.stderr}"


def test_git_guard_blocks_a_shell_form_whose_payload_it_cannot_find(tmp_path):
    """A recognised `-c` form with no payload is unreadable, and unreadable blocks."""
    ev = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": "bash -c"}}
    r = run_hook("git_guard.py", ev, tmp_path)
    assert r.returncode == 2 and "git-guard" in r.stderr


def test_git_guard_still_allows_shell_commands_that_do_not_run_git(tmp_path):
    """The payload is re-parsed, not pattern-matched: benign `-c` work must pass.

    Blocking every `bash -c` would be fail-closed and useless; the fix has to
    distinguish a payload that runs git from one that does not.
    """
    for cmd in ("bash -c 'echo hello'", "sh -c 'ls -la'", "bash scripts/run.sh", "echo 'git push'"):
        ev = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": cmd}}
        r = run_hook("git_guard.py", ev, tmp_path)
        assert r.returncode == 0, f"false positive: {cmd}\n{r.stderr}"


def test_review_gate_blocks_a_push_aimed_at_another_repository(tmp_path):
    """`--git-dir` / `GIT_DIR=` publish from a repo the gate never resolved.

    The global-option parser consumed both and returned only a -C-derived cwd,
    so the gate attested THIS worktree's HEAD while git pushed another
    repository's objects. `-C` stays allowed: its value is honoured.
    """
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    for cmd in (
        f"git --git-dir={tmp_path}/other/.git push origin HEAD",
        f"GIT_DIR={tmp_path}/other/.git git push origin HEAD",
        f"git --work-tree={tmp_path}/other push origin HEAD",
    ):
        r = run_hook(
            "codex_review_gate.py",
            {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": cmd}},
            repo,
            env={"DUAL_REVIEW_REPORTS": str(tmp_path / "reports")},
        )
        assert r.returncode == 2, f"not blocked: {cmd}\n{r.stderr}"


def test_review_gate_still_allows_reading_this_repositorys_git_dir(tmp_path):
    """`git rev-parse --git-dir` asks THIS repo where it is; it publishes nothing.

    Only the global-option span is scanned, so a subcommand flag of the same
    spelling must not be mistaken for a repository selector.
    """
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    r = run_hook(
        "codex_review_gate.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "git rev-parse --git-dir"}},
        repo,
        env={"DUAL_REVIEW_REPORTS": str(tmp_path / "reports")},
    )
    assert r.returncode == 0, r.stderr


def test_review_gate_resolves_the_refspec_when_repo_supplies_the_remote(tmp_path):
    """`git push --repo=origin <refspec>` attested HEAD instead of the refspec.

    positional[0] was discarded unconditionally as the remote, but `--repo`
    already supplied it -- so the first positional is a refspec, and dropping it
    fell through to HEAD. A reviewed HEAD then authorised publishing another ref.
    """
    repo, head = _gated_repo(tmp_path)
    # A second branch carrying its OWN unreviewed gated change.
    _run_git(repo, "checkout", "-q", "-b", "unreviewed", "origin/main")
    (repo / "src" / "hft_platform").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "hft_platform" / "sneaky.py").write_text("value = 2\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "unreviewed work")
    other = _run_git(repo, "rev-parse", "unreviewed")
    _run_git(repo, "checkout", "-q", head)
    _report(tmp_path / "reports", repo, head)  # only HEAD is reviewed
    for cmd in (
        "git push --repo=origin unreviewed:refs/heads/x",
        "git push --repo origin unreviewed:refs/heads/x",
    ):
        r = run_hook(
            "codex_review_gate.py",
            {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": cmd}},
            repo,
            env={"DUAL_REVIEW_REPORTS": str(tmp_path / "reports")},
        )
        assert r.returncode == 2, f"HEAD authorised {other[:8]}: {cmd}\n{r.stderr}"


def test_the_file_that_activates_the_hooks_is_gated_source(tmp_path):
    """Gating the hook implementations but not their registration is not a gate.

    A branch deleting the PreToolUse entries from `.claude/settings.json` touched
    no gated prefix, published freely, and disabled enforcement on checkout.
    """
    sys.path.insert(0, str(HOOKS))
    try:
        import review_attestation
    finally:
        sys.path.pop(0)
    assert ".claude/settings.json" in review_attestation.GATED_PREFIXES
    assert ".claude/settings.json".startswith(review_attestation.GATED_PREFIXES)
    assert not "docs/notes.md".startswith(review_attestation.GATED_PREFIXES)


def test_pre_push_loads_its_verifier_from_the_published_ref(tmp_path):
    """The outgoing branch supplied the verifier that judged its own push.

    `sys.path.insert(root/.claude/hooks)` meant a branch could ship
    `GATED_PREFIXES = ()` or an approving `verify` and that code authorised the
    same push. The verifier is read from origin/main instead: a branch may
    improve it, but not use the improvement before it is reviewed.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    repo = tmp_path / "repo"
    (repo / ".claude" / "hooks").mkdir(parents=True)
    shutil.copy(HOOKS / "review_attestation.py", repo / ".claude" / "hooks" / "review_attestation.py")
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "the real verifier")
    _run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / ".claude" / "hooks" / "review_attestation.py").write_text(
        "GATED_PREFIXES = ()\n"
        "def canonical_range(*a, **k): return None\n"
        "def gated_files(*a, **k): return []\n"
        "def verify(*a, **k): return True\n"
    )
    _run_git(repo, "commit", "-qam", "a verifier that approves everything")

    path = HOOKS.parents[1] / "scripts" / "git-hooks" / "pre-push"
    spec = importlib.util.spec_from_loader("prepush_under_test", SourceFileLoader("prepush_under_test", str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    verifier, why = mod.load_verifier(str(repo))
    assert verifier is not None, why
    assert verifier.GATED_PREFIXES, "the outgoing tree's empty GATED_PREFIXES was used"
    assert "src/" in verifier.GATED_PREFIXES


def test_pre_push_refuses_when_the_published_verifier_is_unreadable(tmp_path):
    """Fail-closed, unlike the Claude-side hook: this one has `--no-verify`."""
    import importlib.util
    from importlib.machinery import SourceFileLoader

    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "commit", "-q", "--allow-empty", "-m", "no verifier here")
    _run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    path = HOOKS.parents[1] / "scripts" / "git-hooks" / "pre-push"
    spec = importlib.util.spec_from_loader("prepush_missing", SourceFileLoader("prepush_missing", str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    verifier, why = mod.load_verifier(str(repo))
    assert verifier is None and "not readable" in why


# --- Adversarial review r6: four more, found after the r5 round ---------------


def test_git_guard_blocks_a_mutation_hidden_in_an_env_split_string(tmp_path):
    """`env -S 'git reset --hard'` was WORSE than the shell form.

    `-S` genuinely is an option-with-a-value, so the arity fix skipped its value
    correctly -- and the value is the entire command. A probe returned no
    invocation AND no unreadable segment, while `env -S 'git rev-parse ...'`
    really does execute git.
    """
    for cmd in (
        "env -S 'git reset --hard HEAD^'",
        "env --split-string='git push origin main'",
        "timeout 5 env -S 'git push'",
    ):
        ev = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": cmd}}
        r = run_hook("git_guard.py", ev, tmp_path)
        assert r.returncode == 2, f"not blocked: {cmd}\n{r.stderr}"

    ok = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": "env -S 'echo hi'"}}
    assert run_hook("git_guard.py", ok, tmp_path).returncode == 0, "benign env -S must still pass"


def test_review_gate_blocks_a_bare_push_whose_refspec_is_configured(tmp_path):
    """A refspec-less `git push` does not necessarily publish HEAD.

    `remote.<name>.push` and `push.default = matching` override it. A probe with
    `remote.probe.push` set had `git push --dry-run` publishing
    `refs/remotes/origin/main` while the gate attested the branch tip.
    """
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    _run_git(repo, "config", "--local", "remote.probe.url", ".")
    _run_git(repo, "config", "--local", "remote.probe.push", "refs/remotes/origin/main:refs/heads/x")
    r = run_hook(
        "codex_review_gate.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "git push probe"}},
        repo,
        env={"DUAL_REVIEW_REPORTS": str(tmp_path / "reports")},
    )
    assert r.returncode == 2 and "remote.probe.push" in r.stderr, r.stderr


def test_review_gate_blocks_a_push_that_rewrites_its_own_push_config(tmp_path):
    """`git -c push.default=matching push` -- the override never reaches `rest`.

    `-c` is consumed as a global option, so the gate saw a plain `git push` and
    resolved HEAD. A push that rewrites its own refspec rules on the command
    line is not resolvable from the command line.
    """
    repo, head = _gated_repo(tmp_path)
    _report(tmp_path / "reports", repo, head)
    r = run_hook(
        "codex_review_gate.py",
        {
            "tool_name": "Bash",
            "cwd": str(repo),
            "tool_input": {"command": "git -c push.default=matching push origin"},
        },
        repo,
        env={"DUAL_REVIEW_REPORTS": str(tmp_path / "reports")},
    )
    assert r.returncode == 2, r.stderr


def test_attestation_is_incomplete_without_a_positive_completion_status(tmp_path):
    """Absence of a known error string is not evidence that the reviewer finished.

    The discriminator was built entirely on a negative, so any UNKNOWN failure --
    a new provider message, a killed process, a lost sidecar -- read as success.
    The launcher now records the reviewer's exit code after it exits, and a
    missing, unreadable, or non-zero status stays incomplete.
    """
    m = _attestation_module()
    for status in ("", "1", "137", "not-a-number"):
        a = m.build_attestation(
            repo_root=str(tmp_path),
            mode="branch",
            pr_number=None,
            native=NATIVE_OK,
            adversarial=ADV_OK,
            adversarial_err=ERR_CLEAN,
            adversarial_status=status,
            base=None,
            head="x",
            now="t",
        )
        assert a["reviewers"]["adversarial"]["completed"] is False, f"status {status!r} counted as finished"
        assert a["complete"] is False


def test_pre_push_refuses_a_rewind_and_a_deletion(tmp_path):
    """The remote OID was discarded, so a force-push inspected clean.

    The gated range is `merge-base(origin/main, local)..local`, which describes
    what SURVIVES a rewind, never what is dropped -- and for a local OID already
    an ancestor of origin/main it is empty. A force-push removing files under
    `src/` therefore returned no gated files at all.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    repo = tmp_path / "repo"
    (repo / ".claude" / "hooks").mkdir(parents=True)
    shutil.copy(HOOKS / "review_attestation.py", repo / ".claude" / "hooks" / "review_attestation.py")
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "one")
    old = _run_git(repo, "rev-parse", "HEAD")
    (repo / "src").mkdir()
    (repo / "src" / "money.py").write_text("value = 1\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "two")
    new = _run_git(repo, "rev-parse", "HEAD")
    _run_git(repo, "update-ref", "refs/remotes/origin/main", new)

    path = HOOKS.parents[1] / "scripts" / "git-hooks" / "pre-push"
    spec = importlib.util.spec_from_loader("prepush_rewind", SourceFileLoader("prepush_rewind", str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Premise: the range for the rewind target is empty, so the old check saw nothing.
    verifier, _ = mod.load_verifier(str(repo))
    rng = verifier.canonical_range(old, cwd=str(repo))
    assert verifier.gated_files(rng, cwd=str(repo)) == [], "premise: a rewind shows no gated files"

    zero = "0" * 40
    rewind = f"refs/heads/main {old} refs/heads/main {new}"
    delete = f"(delete) {zero} refs/heads/main {new}"
    for stdin_line, what in ((rewind, "rewind"), (delete, "deletion")):
        r = subprocess.run(
            [sys.executable, str(path)],
            cwd=repo,
            input=stdin_line + "\n",
            capture_output=True,
            text=True,
        )
        assert r.returncode == 1, f"{what} was allowed:\n{r.stdout}{r.stderr}"


def test_git_guard_blocks_a_mutation_behind_a_shell_c_separator(tmp_path):
    """`bash -c -- 'git reset --hard'` runs -- verified against bash itself.

    The payload was taken as the token immediately after `-c`, which selected
    `--` (or `-x`), flattened to a segment containing no git, and reported
    neither an invocation nor an unreadable segment.
    """
    for cmd in (
        "bash -c -- 'git reset --hard HEAD^'",
        "sh -c -- 'git push origin HEAD'",
        "bash -c -x 'git push'",
    ):
        ev = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": cmd}}
        r = run_hook("git_guard.py", ev, tmp_path)
        assert r.returncode == 2, f"not blocked: {cmd}\n{r.stderr}"

    # A separator with nothing after it is unreadable, and unreadable blocks.
    ev = {"agent_type": "hft-executor", "tool_name": "Bash", "tool_input": {"command": "bash -c --"}}
    assert run_hook("git_guard.py", ev, tmp_path).returncode == 2


def test_a_newer_blocking_review_is_not_outvoted_by_an_older_approval(tmp_path):
    """Re-reviewing and FINDING a defect has to revoke the earlier approval.

    `verify` scans newest-first but only recorded a rejection and kept going, so
    an older approving attestation for the same head still returned success --
    the exact commit could publish while its newest high-severity finding sat
    unacknowledged.
    """
    repo, head = _gated_repo(tmp_path)
    reports = tmp_path / "reports"
    # Older: a clean approval of this exact head.
    _report(reports, repo, head, name="20260101-000000-old-approval", verdict="approve")
    # Newer: the same head, same diff, now with an unacknowledged high finding.
    _report(
        reports,
        repo,
        head,
        name="20260102-000000-new-blocking",
        verdict="needs-attention",
        adversarial_body="Findings:\n- [high] the money path is wrong (src/x.py:1)\n",
    )
    r = run_hook(
        "codex_review_gate.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "git push origin HEAD"}},
        repo,
        env={"DUAL_REVIEW_REPORTS": str(reports)},
    )
    assert r.returncode == 2, f"the older approval outvoted the newer blocking review\n{r.stderr}"
    assert "new-blocking" in r.stderr, r.stderr
