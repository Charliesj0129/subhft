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


#: Push options that consume the next token. Without these, `git push -o ci.skip`
#: reads "ci.skip" as a refspec and blocks a legitimate push.
_PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}

#: Selectors that publish refs beyond the ones named on the command line. There
#: is no single head to attest, so they fail closed rather than being checked
#: against HEAD and passing while other refs go out unreviewed.
_MULTI_REF = {"--all", "--branches", "--mirror", "--tags", "--follow-tags"}


def _is_delete(rest: list[str]) -> bool:
    """A deletion removes a ref and publishes no source."""
    return "--delete" in rest or "-d" in rest


def _push_positionals(rest: list[str]) -> list[str]:
    """Positional arguments of `git push`, with option values consumed."""
    out: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-"):
            i += 2 if tok in _PUSH_OPTS_WITH_VALUE else 1
            continue
        out.append(tok)
        i += 1
    return out


def _push_heads(rest: list[str], cwd: str | None) -> tuple[list[str], list[str]]:
    """(resolved head OIDs, selectors that could not be resolved).

    `git push` with no refspec publishes the current branch; with refspecs it
    publishes each source side. A `:dst` refspec with an empty source is a
    delete and publishes nothing.
    """
    multi = sorted(set(rest) & _MULTI_REF)
    if multi:
        return [], [f"{' '.join(multi)} (publishes refs this gate cannot enumerate)"]

    positional = _push_positionals(rest)
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


def _targets(cmd: str, event_cwd: str | None) -> tuple[list[tuple[str, str | None]], list[str]]:
    """[(head_oid, repo cwd)] this command would publish, plus unresolved selectors."""
    targets: list[tuple[str, str | None]] = []
    unresolved: list[str] = []

    for sub, rest, cwd_override in git_invocations(cmd):
        if sub != "push" or _is_delete(rest):
            continue
        # `git -C <dir> push` publishes THAT worktree's HEAD, not this one's.
        cwd = cwd_override or event_cwd
        heads, bad = _push_heads(rest, cwd)
        targets += [(h, cwd) for h in heads]
        unresolved += bad

    for group, sub, rest in gh_invocations(cmd):
        if group != "pr" or sub not in ("create", "merge"):
            continue
        if sub == "create":
            # A PR is opened from a local branch: --head names it, else HEAD.
            ref = _option_value(rest, "--head") or "HEAD"
            oid = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=event_cwd)
            if oid:
                targets.append((oid, event_cwd))
            else:
                unresolved.append(f"gh pr create --head {ref}")
            continue
        selector = next((tok for tok in rest if not tok.startswith("-")), "")
        oid = _gh_pr_head(selector, event_cwd)
        if oid:
            targets.append((oid, event_cwd))
        else:
            unresolved.append(f"gh pr merge {selector or '(current branch)'}")

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
