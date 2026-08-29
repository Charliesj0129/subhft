#!/usr/bin/env python3
"""PreToolUse(Bash) gate: a source change reaches a remote only after a Codex review.

Why a hook and not a habit
--------------------------
Until 2026-08-26 "every deliverable gets both Codex reviews" was a behavioural
commitment with nothing behind it. It was honoured because it was remembered,
which makes its reliability equal to the reliability of remembering.

What it gates
-------------
Commands that publish work: ``git push``, ``gh pr create``, ``gh pr merge``.
Local commits are deliberately NOT gated -- committing is how work is kept safe,
and blocking it would push the author toward leaving work uncommitted, which is
the more dangerous failure. Publishing is the edge worth gating.

It fires only when the outgoing commits touch ``src/``, ``rust_core/`` or
``config/``. Docs, tests, research and archive branches push freely.

What changed 2026-08-29
-----------------------
The first version matched ``\\bgit\\s+push\\b`` on the raw command string and
credited any report directory whose ``REVIEWED_SHA`` equalled HEAD and which held
a ``Verdict:`` line. A Codex adversarial pass on that hook returned NO SHIP, and
all four of its findings reproduced:

* ``git -C <dir> push``, ``git -c k=v push`` and ``gh --repo X pr merge N`` did
  not match the regex at all -> now parsed structurally by
  ``hook_common.git_invocations`` / ``gh_invocations``.
* HEAD is not what a push sends. ``git push origin other-branch`` and
  ``gh pr merge <N>`` publish something else entirely -> the head OID is now
  resolved per refspec, and for a PR from its live head.
* 24 of 25 historical reports say ``needs-attention``, and every one of them
  opened the gate -> ``review_attestation`` requires both reviewers to have
  finished and an ACK naming each high finding.
* ``--name-only`` hides a rename's source path, so moving ``src/x.py`` out of
  ``src/`` showed no gated prefix -> ``--no-renames`` throughout.

Escape hatch
------------
``CODEX_REVIEW_OVERRIDE=1 git push ...`` proceeds and says so. Deliberate,
visible, and attributable; not a silent bypass.

Failure mode
------------
Two different answers, on purpose:

* an unexpected exception ALLOWS. A gate that wedges every push when its own
  parser breaks costs more than the reviews it protects -- see the relative-path
  hook incident that blocked Bash and Edit simultaneously.
* a refspec that is present but unresolvable BLOCKS. Not knowing what is being
  pushed is precisely the state that must not pass, and it is a condition this
  code recognises rather than one it tripped over.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hook_common import gh_invocations, git_invocations, read_event  # noqa: E402
from review_attestation import GATED_PREFIXES, canonical_range, gated_files, git, verify  # noqa: E402

OVERRIDE = "CODEX_REVIEW_OVERRIDE"


def _is_push(rest: list[str]) -> bool:
    """A push that publishes objects. Deletions remove a ref and add no source."""
    return "--delete" not in rest and "-d" not in rest


def _push_heads(rest: list[str], cwd: str | None) -> tuple[list[str], list[str]]:
    """(resolved head OIDs, refspecs that could not be resolved).

    ``git push`` with no refspec publishes the current branch; with refspecs it
    publishes each source side. A ``:dst`` refspec with an empty source is a
    delete and publishes nothing.
    """
    positional = [t for t in rest if not t.startswith("-")]
    refspecs = positional[1:]  # positional[0] is the remote
    if not refspecs:
        head = git("rev-parse", "HEAD", cwd=cwd)
        return ([head] if head else []), ([] if head else ["HEAD"])

    heads: list[str] = []
    unresolved: list[str] = []
    for spec in refspecs:
        src = spec.split(":", 1)[0].lstrip("+")
        if not src:
            continue  # ":dst" is a delete
        oid = git("rev-parse", "--verify", "--quiet", f"{src}^{{commit}}", cwd=cwd)
        (heads if oid else unresolved).append(oid or spec)
    return heads, unresolved


def _pr_head(number: str, cwd: str | None) -> str:
    import json
    import subprocess

    out = subprocess.run(
        ["gh", "pr", "view", number, "--json", "headRefOid"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if out.returncode != 0:
        return ""
    try:
        return str(json.loads(out.stdout).get("headRefOid") or "")
    except ValueError:
        return ""


def _targets(cmd: str, cwd: str | None) -> tuple[list[tuple[str, int | None]], list[str]]:
    """[(head_oid, pr_number)] this command would publish, plus unresolved refspecs."""
    targets: list[tuple[str, int | None]] = []
    unresolved: list[str] = []

    for sub, rest in git_invocations(cmd):
        if sub == "push" and _is_push(rest):
            heads, bad = _push_heads(rest, cwd)
            targets += [(h, None) for h in heads]
            unresolved += bad

    for group, sub, rest in gh_invocations(cmd):
        if group != "pr" or sub not in ("create", "merge"):
            continue
        if sub == "create":
            head = git("rev-parse", "HEAD", cwd=cwd)
            (targets.append((head, None)) if head else unresolved.append("HEAD"))
            continue
        number = next((t for t in rest if t.isdigit()), "")
        if not number:
            # `gh pr merge` with no number merges the PR for the current branch.
            head = git("rev-parse", "HEAD", cwd=cwd)
            (targets.append((head, None)) if head else unresolved.append("HEAD"))
            continue
        oid = _pr_head(number, cwd)
        (targets.append((oid, int(number))) if oid else unresolved.append(f"PR #{number}"))

    return targets, unresolved


def _warn_missing_pre_push(cwd: str | None) -> None:
    """The other enforcement point is a git hook; say so when it is not installed."""
    root = git("rev-parse", "--show-toplevel", cwd=cwd)
    hooks = git("rev-parse", "--git-path", "hooks", cwd=cwd)
    if not root or not hooks:
        return
    path = Path(hooks) if os.path.isabs(hooks) else Path(root) / hooks
    if not (path / "pre-push").exists():
        print(
            "codex_review_gate: note -- .git/hooks/pre-push is not installed, so pushes made "
            "outside this session are ungated. Install with: make install-git-hooks",
            file=sys.stderr,
        )


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
        _warn_missing_pre_push(cwd)

        if unresolved:
            print(
                "BLOCKED by codex_review_gate: cannot resolve what this command would publish "
                f"({', '.join(unresolved)}).\n"
                "  A gate that cannot name the objects leaving the repository must not pass them.\n"
                f"  Deliberate bypass:  {OVERRIDE}=1 <command>",
                file=sys.stderr,
            )
            return 2

        for head, pr in targets:
            rng = canonical_range(head, cwd=cwd)
            if rng is None:
                continue  # no origin/main -> not this project's layout
            touched = gated_files(rng, cwd=cwd)
            if not touched:
                continue
            decision = verify(cwd, head, pr_number=pr)
            if decision.ok:
                print(f"codex_review_gate: {decision.reason}", file=sys.stderr)
                continue
            print(
                "BLOCKED by codex_review_gate: this push/PR carries "
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
