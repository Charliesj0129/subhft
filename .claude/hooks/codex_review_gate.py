#!/usr/bin/env python3
"""PreToolUse(Bash) gate: a source change is merged only after a Codex review.

Why a hook and not a habit
--------------------------
Until 2026-08-26 "every deliverable gets both Codex reviews" was a behavioural
commitment with nothing behind it. It was honoured because it was remembered,
which makes its reliability equal to the reliability of remembering.

What it gates
-------------
One command: ``gh pr merge``. The merge is the step that lands work on main.
``git push`` and ``gh pr create`` are NOT gated: they put a branch where CI and
reviewers can see it, and local commits stay ungated for the same reason as
before -- blocking the ways work is kept safe pushes the author toward leaving
it unpublished.

It fires only when the PR's commits touch a prefix in
``review_attestation.GATED_PREFIXES``: ``src/``, ``rust_core/``, ``config/``,
and the gate's own code and registration (``.claude/hooks/``,
``.claude/settings.json``). Docs, tests and research merge freely.

How a merge is resolved
-----------------------
* ``gh --repo X pr merge N`` is parsed structurally by
  ``hook_common.gh_invocations``, not matched as text, so global options do not
  hide it. A ``--repo`` that is not this checkout's repository blocks: its
  review cannot be verified from here.
* The head is asked of GitHub, never taken from local HEAD, and the command must
  pin it with ``--match-head-commit``; a head that advanced after the check
  would otherwise merge unreviewed.
* ``review_attestation`` requires both reviewers to have finished and an ACK
  naming each high finding; a ``needs-attention`` verdict alone opens nothing.
* ``--no-renames`` throughout, so moving ``src/x.py`` out of ``src/`` still
  shows a gated path.

Escape hatch
------------
``CODEX_REVIEW_OVERRIDE=1 gh pr merge ...`` proceeds and says so. Deliberate,
visible, and attributable; not a silent bypass.

Failure mode
------------
Two different answers, on purpose:

* an unexpected exception ALLOWS. A gate that wedges every merge when its own
  parser breaks costs more than the reviews it protects -- see the relative-path
  hook incident that blocked Bash and Edit simultaneously.
* a merge whose head cannot be resolved or is not pinned BLOCKS, and so does a
  segment naming both ``gh`` and ``merge`` that the parser could not read. Not
  knowing what is being merged is precisely the state that must not pass.

Honest limits
-------------
A merge through ``gh api``, the GitHub web UI, or another machine never reaches
this hook. It is a floor against forgetting, not a control against intent.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hook_common import gh_invocations, read_event, unattributed_segments  # noqa: E402
from review_attestation import GATED_PREFIXES, canonical_range, gated_files, git, verify  # noqa: E402

OVERRIDE = "CODEX_REVIEW_OVERRIDE"

#: An unreadable segment concerns this gate only if it could be the merge
#: itself: it names `gh` and `merge` as words of their own. `git merge-base`
#: inside a shape the parser cannot read is not one.
_GH_WORD = re.compile(r"(?<![\w.-])gh(?![\w-])")
_MERGE_WORD = re.compile(r"(?<![\w-])merge(?![\w-])")


def _gh_pr_head(selector: str, cwd: str | None) -> str:
    """The PR head OID GitHub would merge, for a number, branch, or URL.

    Always asked of GitHub, never inferred from local HEAD: a merge happens
    server-side, so a PR whose head advanced remotely would otherwise be
    authorised by a review of whatever this checkout happens to be on.
    """
    import json
    import subprocess

    args = ["gh", "pr", "view"] + ([selector] if selector else []) + ["--json", "headRefOid"]
    out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30, check=False)
    if out.returncode != 0:
        return ""
    try:
        return str(json.loads(out.stdout).get("headRefOid") or "")
    except ValueError:
        return ""


def _option_value(rest: list[str], name: str) -> str:
    for i, tok in enumerate(rest):
        if tok == name and i + 1 < len(rest):
            return rest[i + 1]
        if tok.startswith(f"{name}="):
            return tok.split("=", 1)[1]
    return ""


def _local_repo_names(cwd: str | None) -> set[str]:
    """`owner/name` for every remote of this checkout, lowercased.

    Used to decide whether a `gh --repo X` command is acting on the repository
    whose review evidence the gate is about to check, or on a different one.
    """
    out: set[str] = set()
    raw = git("remote", "-v", cwd=cwd)
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        url = parts[1].removesuffix(".git")
        url = url.split(":")[-1] if url.startswith("git@") else url
        bits = [b for b in url.split("/") if b]
        if len(bits) >= 2:
            out.add("/".join(bits[-2:]).lower())
    return out


def _targets(cmd: str, event_cwd: str | None) -> tuple[list[tuple[str, str | None]], list[str]]:
    """[(head_oid, repo cwd)] this command would merge, plus unresolved selectors."""
    targets: list[tuple[str, str | None]] = []
    unresolved: list[str] = []

    for seg in unattributed_segments(cmd):
        # Fail closed only where the unread segment could be the merge: a merge
        # this parser cannot read is exactly the state that must not pass.
        if _GH_WORD.search(seg) and _MERGE_WORD.search(seg):
            unresolved.append(f"unreadable command: {seg}")

    for group, sub, rest, repo in gh_invocations(cmd):
        if group != "pr" or sub != "merge":
            continue
        if repo and repo.lower().removesuffix(".git") not in _local_repo_names(event_cwd):
            # `gh` would act on a repository this checkout is not. The diff hash
            # can only be recomputed against a local worktree, so a foreign
            # --repo can never be verified -- and silently resolving the
            # selector here would attest THIS repository's PR instead.
            unresolved.append(f"gh --repo {repo} (not this repository; cannot verify its review)")
            continue
        matched = _option_value(rest, "--match-head-commit")
        if not matched:
            unresolved.append(
                "gh pr merge without --match-head-commit (the head can advance between "
                "this check and the merge GitHub performs)"
            )
            continue
        selector = next((tok for tok in rest if not tok.startswith("-")), "")
        oid = _gh_pr_head(selector, event_cwd)
        if not oid:
            unresolved.append(f"gh pr merge {selector or '(current branch)'}")
        elif oid != matched:
            # The OID the command pins is not the head GitHub reports now.
            unresolved.append(f"gh pr merge --match-head-commit {matched} but PR head is {oid}")
        else:
            targets.append((oid, event_cwd))

    return targets, unresolved


def main() -> int:
    event = read_event()
    cmd = str((event.get("tool_input") or {}).get("command", ""))
    if not cmd:
        return 0

    cwd = event.get("cwd") or os.getcwd()
    try:
        targets, unresolved = _targets(cmd, cwd)
    except Exception as exc:  # never wedge the session on this gate's own bug
        print(f"codex_review_gate: allowing, internal error: {exc}", file=sys.stderr)
        return 0
    if not targets and not unresolved:
        return 0

    if os.environ.get(OVERRIDE) == "1" or f"{OVERRIDE}=1" in cmd:
        print(f"codex_review_gate: overridden explicitly by {OVERRIDE}=1", file=sys.stderr)
        return 0

    try:
        if unresolved:
            print(
                "BLOCKED by codex_review_gate: cannot resolve what this command would merge "
                f"({', '.join(unresolved)}).\n"
                "  A gate that cannot name the commits being merged must not pass them.\n"
                f"  Deliberate bypass:  {OVERRIDE}=1 <command>",
                file=sys.stderr,
            )
            return 2

        for head, target_cwd in targets:
            rng = canonical_range(head, cwd=target_cwd)
            if rng is None:
                if not git("rev-parse", "--verify", "--quiet", "origin/main", cwd=target_cwd):
                    continue  # no origin/main -> not this project's layout
                # origin/main exists but the range does not: the object is not in
                # this checkout. A PR head that was never fetched must not slip
                # through as "nothing to gate".
                print(
                    "BLOCKED by codex_review_gate: "
                    f"{head[:8]} is not present locally, so its changes cannot be inspected.\n"
                    "  Fetch it first: git fetch origin, or gh pr checkout <N>.\n"
                    f"  Deliberate bypass:  {OVERRIDE}=1 <command>",
                    file=sys.stderr,
                )
                return 2
            touched = gated_files(rng, cwd=target_cwd)
            if not touched:
                continue
            decision = verify(target_cwd, head)
            if decision.ok:
                print(f"codex_review_gate: {decision.reason}", file=sys.stderr)
                continue
            print(
                "BLOCKED by codex_review_gate: this merge carries "
                f"{len(touched)} change(s) under {', '.join(GATED_PREFIXES)}.\n"
                f"  {decision.reason}\n"
                f"  Deliberate bypass:  {OVERRIDE}=1 <command>",
                file=sys.stderr,
            )
            return 2
    except Exception as exc:  # same reason as above: allow rather than wedge
        print(f"codex_review_gate: allowing, internal error: {exc}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
