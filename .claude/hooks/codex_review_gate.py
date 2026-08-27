#!/usr/bin/env python3
"""PreToolUse(Bash) gate: a source change reaches a remote only after a Codex review.

Why a hook and not a habit
--------------------------
Until 2026-08-26 "every deliverable gets both Codex reviews" was a behavioural
commitment with nothing behind it. It was honoured because it was remembered,
which makes its reliability equal to the reliability of remembering. The two
reviews it produced on PR #460 each found a defect the author had shipped --
one of them an inverted safety argument -- so the value is measured, and a
control whose only enforcement is memory is not a control.

What it gates
-------------
Commands that publish work: ``git push``, ``gh pr create``, ``gh pr merge``.
Local commits are deliberately NOT gated -- committing is how work is kept
safe, and blocking it would push the author toward leaving work uncommitted,
which is the more dangerous failure. Publishing is the edge worth gating.

It only fires when the outgoing commits touch ``src/`` or ``rust_core/``. Docs,
config, tests and archive branches push freely.

How a review is credited
------------------------
``~/.claude/bin/dual-review.sh`` writes ``REVIEWED_SHA`` into each report
directory. A report counts only when that sha equals the current HEAD, so
amending or adding a commit after a review invalidates it -- which is the point.

It must ALSO contain a reviewer's ``Verdict:`` line. The stamp is written when
the run is launched, not when it finishes, so a reviewer that dies mid-run
leaves a stamped directory holding nothing but an error -- and crediting that
would be crediting a review nobody performed. Measured on 2026-08-27: both
reviewers hit a Codex usage limit, the stamp matched HEAD, and the gate cleared.

Escape hatch
------------
``CODEX_REVIEW_OVERRIDE=1 git push ...`` proceeds and says so. Deliberate,
visible, and attributable; not a silent bypass.

Failure mode
------------
Any internal error allows the command. A gate that wedges every push when its
own parsing breaks costs more than the reviews it protects -- see the
relative-path hook incident that blocked Bash and Edit simultaneously.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPORTS = Path.home() / ".claude" / "review-reports"
GATED = re.compile(r"\bgit\s+push\b|\bgh\s+pr\s+(create|merge)\b")
SOURCE_PREFIXES = ("src/", "rust_core/")


def _git(*args: str, cwd: str | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def _outgoing_touches_source(cwd: str | None) -> bool:
    """True when commits not yet on the upstream/main touch platform source."""
    if not _git("rev-parse", "--verify", "--quiet", "origin/main", cwd=cwd):
        # Not this project's layout. A gate that blocks pushes in every
        # unrelated repository on the machine gets disabled, and then it
        # protects nothing.
        return False
    base = _git("merge-base", "origin/main", "HEAD", cwd=cwd)
    if not base:
        return True  # this repo, but the range is unreadable -> gate
    files = _git("diff", "--name-only", f"{base}...HEAD", cwd=cwd)
    return any(f.startswith(SOURCE_PREFIXES) for f in files.splitlines())


def _has_a_verdict(report_dir: Path) -> bool:
    """True when at least one reviewer in this directory actually reported.

    ``dual-review.sh`` stamps ``REVIEWED_SHA`` when it *launches*, before either
    reviewer has said anything -- it has to, so the directory is findable while
    the run is in flight. A reviewer that then dies (usage limit, network,
    upstream error) leaves a report saying so, and matching the sha alone would
    credit that as a review. On 2026-08-27 both reviewers hit a Codex usage
    limit; the stamp was still there, and the push cleared a gate no reviewer
    had ever looked at.

    An empty review reads like an approval. It is not one, and the whole reason
    this gate exists is that a control enforced by memory is not a control -- so
    the credit has to rest on evidence a reviewer produced, not on a file that
    proves only that one was started.
    """
    for report in sorted(report_dir.glob("*.md")):
        try:
            text = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(line.startswith("Verdict:") for line in text.splitlines()):
            return True
    return False


def _reviewed(sha: str) -> Path | None:
    if not sha or not REPORTS.is_dir():
        return None
    for d in sorted(REPORTS.iterdir(), reverse=True):
        stamp = d / "REVIEWED_SHA"
        try:
            if not (stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == sha):
                continue
        except OSError:
            continue
        if _has_a_verdict(d):
            return d
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    cmd = str((data.get("tool_input") or {}).get("command", ""))
    if not GATED.search(cmd):
        return 0
    if os.environ.get("CODEX_REVIEW_OVERRIDE") == "1" or "CODEX_REVIEW_OVERRIDE=1" in cmd:
        print("codex_review_gate: overridden explicitly by CODEX_REVIEW_OVERRIDE=1", file=sys.stderr)
        return 0

    try:
        cwd = (data.get("cwd") or os.getcwd()) or None
        m = re.search(r"--cwd[= ]+(\S+)", cmd)
        if m:
            cwd = m.group(1)
        if not _outgoing_touches_source(cwd):
            return 0
        sha = _git("rev-parse", "HEAD", cwd=cwd)
        report = _reviewed(sha)
    except Exception as exc:  # never wedge the session on this gate's own bug
        print(f"codex_review_gate: allowing, internal error: {exc}", file=sys.stderr)
        return 0

    if report is not None:
        print(f"codex_review_gate: {sha[:8]} reviewed ({report.name})", file=sys.stderr)
        return 0

    print(
        "BLOCKED by codex_review_gate: this push/PR carries changes under src/ or "
        f"rust_core/, and no completed Codex review covers HEAD ({sha[:8]}).\n"
        "  A report directory stamped with this sha but holding no `Verdict:` line does\n"
        "  NOT count -- the reviewer was launched and did not report.\n"
        "  Run:  /dual-review        (or ~/.claude/bin/dual-review.sh --cwd <repo>)\n"
        "  Then re-run the command; the report stamps REVIEWED_SHA and this gate clears.\n"
        "  A review taken before the last commit does not count -- re-review after amending.\n"
        "  Deliberate bypass:  CODEX_REVIEW_OVERRIDE=1 <command>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
