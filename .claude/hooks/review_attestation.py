#!/usr/bin/env python3
"""Credit a Codex review only when it provably covers the bytes being published.

The problem this replaces
-------------------------
``dual-review.sh`` stamps ``REVIEWED_SHA`` when it *launches* the two reviewers,
so that stamp proves a review was started and nothing more. The gate built on it
credited a directory holding any ``Verdict:`` line, and measurement over all 25
report directories in ``~/.claude/review-reports/`` showed what that admits:

===========================================  =======
signal                                        dirs
===========================================  =======
``review.md`` carries a ``Verdict:`` line      0 / 25
``adversarial.md`` carries one                22 / 25
native reviewer actually reported             16 / 25
adversarial verdict is ``approve``             1 / 25
===========================================  =======

The native reviewer emits free-form prose and *never* a ``Verdict:`` line, so a
"both reviewers" rule keyed on that string is a one-reviewer rule -- and 24 of the
25 verdicts are ``needs-attention``, i.e. the reviewer said NO SHIP and the gate
opened anyway.

What is credited now
--------------------
An ``ATTESTATION.json`` written **after both reviewers finish**, plus:

* the reviewed head OID equals the OID actually being published,
* the diff hash recomputed now equals the one recorded then (so amending,
  or reviewing a narrower base, invalidates the evidence),
* a working-tree review never credits a publish -- uncommitted files are not
  what a push sends,
* ``needs-attention`` requires an ``ACK.md`` naming every high-severity finding.

Every rule re-derives its facts from git rather than trusting the attestation's
own copy. The producer lives outside this repository and is not covered by its
CI; the consumer must not inherit that gap.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPORTS = Path(os.environ.get("DUAL_REVIEW_REPORTS") or (Path.home() / ".claude" / "review-reports"))

#: Changes under these prefixes may not reach a remote unreviewed. ``config/`` is
#: here because the two most expensive defects of 2026-08 were config, not code:
#: the daily-loss limit enforced 10x too tight, and an expired contract seed in
#: ``strategies.yaml`` that left a strategy bound to nothing.
GATED_PREFIXES = ("src/", "rust_core/", "config/")

#: Severities that must be named in ACK.md before a ``needs-attention`` review can
#: publish. Medium and low are deliberately exempt -- friction disproportionate to
#: severity gets routed around, and a gate nobody uses protects nothing.
ACK_SEVERITIES = ("critical", "high")

#: The four bodies ``render.mjs`` emits when the native reviewer produced nothing.
#: ``review.md`` is 95 bytes in that state; a real report is kilobytes.
NATIVE_FAILURES = (
    "Reviewer failed to output a response.",
    "Codex review failed.",
    "Codex review completed without any stdout output.",
    "Codex did not return valid structured JSON.",
)

_VERDICT = re.compile(r"^Verdict:\s*(approve|needs-attention)\s*$", re.M)
_ADV_FINDING = re.compile(r"^-\s*\[(critical|high|medium|low)\]\s*(.+?)\s*$", re.M | re.I)
_NATIVE_FINDING = re.compile(r"^-\s*\[(P\d)\]\s*(.+?)(?:\s+—\s+|\s+--\s+|$)", re.M)


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str
    report_dir: Path | None = None


def git(*args: str, cwd: str | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def canonical_range(head: str, cwd: str | None = None) -> str | None:
    """The one range a review of ``head`` is expected to have covered.

    Producer and consumer must agree exactly, so this is the only place either
    decides what "the diff" means.
    """
    base = git("merge-base", "origin/main", head, cwd=cwd)
    return f"{base}...{head}" if base else None


def diff_sha256(head: str, cwd: str | None = None) -> str | None:
    """sha256 of the canonical diff.

    ``--no-renames`` matters twice. It is what makes the hash stable across git
    versions and rename-detection thresholds, and it is what stops a source file
    *moved out of* a gated directory from disappearing: with rename detection
    ``--name-only`` reports only the destination path, so moving ``src/x.py`` to
    ``docs/x.py`` deletes production source while showing the gate no ``src/``.
    """
    rng = canonical_range(head, cwd=cwd)
    if not rng:
        return None
    out = subprocess.run(
        ["git", "diff", "--no-renames", "--full-index", rng],
        cwd=cwd,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if out.returncode != 0:
        return None
    return hashlib.sha256(out.stdout).hexdigest()


def gated_files(rng: str, cwd: str | None = None) -> list[str]:
    """Files in ``rng`` that live under a gated prefix, renames counted on both sides."""
    files = git("diff", "--name-only", "--no-renames", rng, cwd=cwd)
    return [f for f in files.splitlines() if f.startswith(GATED_PREFIXES)]


def native_state(text: str) -> tuple[bool, str | None]:
    """(completed, failure sentence) for the native reviewer.

    `/codex:review` renders free-form prose and NEVER a `Verdict:` line -- true
    of all 25 historical reports -- so completion has to be read as the absence
    of the four bodies render.mjs writes when nothing came back. `review.md` is
    95 bytes in that state; a real report is kilobytes.
    """
    if not text.strip():
        return False, "no output"
    for sentence in NATIVE_FAILURES:
        if sentence in text:
            return False, sentence
    return True, None


def adversarial_state(text: str) -> tuple[bool, str | None]:
    """(completed, verdict) for the adversarial reviewer.

    `/codex:adversarial-review` is schema-constrained
    (schemas/review-output.schema.json, verdict enum approve|needs-attention) and
    the line is rendered only once that JSON parsed, so its presence IS the
    completion signal. Do NOT substitute `.err`'s `Turn completed.`:
    20260827-224921-dailyloss-r3 has `Turn failed` in the sidecar and a valid
    `Verdict: approve` with a real body in the report.
    """
    m = _VERDICT.search(text)
    return (True, m.group(1)) if m else (False, None)


def all_findings(native: str, adversarial: str) -> list[dict]:
    """Every finding both reviewers reported, normalised into one shape."""
    out: list[dict] = []
    for sev, title in _ADV_FINDING.findall(adversarial):
        out.append(
            {
                "reviewer": "adversarial",
                "severity": sev.lower(),
                "title": re.sub(r"\s*\([^)]*:\d+[-\d]*\)\s*$", "", title),
            }
        )
    for sev, title in _NATIVE_FINDING.findall(native):
        out.append({"reviewer": "review", "severity": sev.upper(), "title": title})
    return out


def build_attestation(
    *, repo_root: str, mode: str, pr_number: int | None, native: str, adversarial: str, head: str, now: str
) -> dict:
    """The attestation payload. Pure apart from the git calls it needs for the hash.

    Lives here, not in the launcher, because the launcher sits outside this
    repository and outside its CI. The consumer must never be the only tested
    half of a contract.
    """
    native_ok, native_failure = native_state(native)
    adv_ok, verdict = adversarial_state(adversarial)
    base = None if mode == "working-tree" else git("merge-base", "origin/main", head, cwd=repo_root)
    sha = None if mode == "working-tree" else diff_sha256(head, cwd=repo_root)
    return {
        "schema": 1,
        "written_at": now,
        "repo_root": repo_root,
        "mode": mode,
        "base_oid": base or None,
        "head_oid": head,
        "pr_number": pr_number,
        "diff_sha256": sha,
        "reviewers": {
            "review": {"completed": native_ok, "failure": native_failure},
            "adversarial": {"completed": adv_ok, "verdict": verdict},
        },
        "findings": all_findings(native, adversarial),
        # A working-tree review can never credit a publish, so it is never
        # complete for the gate's purposes even when both reviewers reported.
        "complete": bool(native_ok and adv_ok and mode != "working-tree" and sha),
    }


def _normalise(title: str) -> str:
    """Compare finding titles loosely enough to survive a re-render."""
    return re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()


def findings_needing_ack(report_dir: Path) -> list[tuple[str, str]]:
    """(severity, title) for every finding an ACK must name."""
    out: list[tuple[str, str]] = []
    adv = report_dir / "adversarial.md"
    if adv.is_file():
        text = adv.read_text(encoding="utf-8", errors="replace")
        for sev, title in _ADV_FINDING.findall(text):
            if sev.lower() in ACK_SEVERITIES:
                out.append((sev.lower(), re.sub(r"\s*\([^)]*:\d+[-\d]*\)\s*$", "", title)))
    native = report_dir / "review.md"
    if native.is_file():
        text = native.read_text(encoding="utf-8", errors="replace")
        for sev, title in _NATIVE_FINDING.findall(text):
            if sev.upper() == "P1":
                out.append(("P1", title))
    return out


def _ack_covers(report_dir: Path, required: list[tuple[str, str]]) -> tuple[bool, str]:
    ack = report_dir / "ACK.md"
    if not ack.is_file():
        titles = "\n".join(f"    - [{s}] {t}" for s, t in required)
        return False, (
            f"the adversarial reviewer returned needs-attention and {report_dir}/ACK.md does not exist.\n"
            f"  Write it naming each finding you are shipping over, one entry plus a reason each:\n{titles}"
        )
    body = ack.read_text(encoding="utf-8", errors="replace")
    entries = _normalise(body)
    missing = [f"[{s}] {t}" for s, t in required if _normalise(t) not in entries]
    if missing:
        return False, "ACK.md does not name these findings:\n" + "\n".join(f"    - {m}" for m in missing)
    # A named finding with no reasoning is a checkbox, not a decision.
    unreasoned = [f"[{s}] {t}" for s, t in required if len(_reason_for(body, t)) < 20]
    if unreasoned:
        listed = "\n".join(f"    - {m}" for m in unreasoned)
        return False, f"ACK.md names these findings but gives no reason (>=20 chars) for shipping over them:\n{listed}"
    return True, ""


def _reason_for(body: str, title: str) -> str:
    """Text following the line that names ``title``, up to the next entry."""
    lines = body.splitlines()
    want = _normalise(title)
    for i, line in enumerate(lines):
        if want and want in _normalise(line):
            reason: list[str] = []
            for nxt in lines[i + 1 :]:
                if re.match(r"^\s*-\s*\[", nxt):
                    break
                reason.append(nxt.strip())
            return " ".join(reason).strip()
    return ""


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _rejection(att: dict, d: Path, repo_root: str | None, head_oid: str, pr_number: int | None) -> str | None:
    """Why this attestation cannot credit the publish, or None when it can.

    Split out of ``verify`` so each rule reads as one clause. Order matters: the
    cheap structural checks run before the diff is recomputed.
    """
    if not att.get("complete"):
        return f"{d.name}: the review never finished (complete=false)"
    if att.get("mode") == "working-tree":
        return (
            f"{d.name}: reviewed the working tree, not commits. Uncommitted files are not "
            "what a push publishes -- re-review the branch."
        )

    reviewers = att.get("reviewers") or {}
    dead = [n for n in ("review", "adversarial") if not (reviewers.get(n) or {}).get("completed")]
    if dead:
        return (
            f"{d.name}: {', '.join(dead)} produced no report. Both reviewers must finish -- "
            "a launched review is not a completed one."
        )

    recomputed = diff_sha256(head_oid, cwd=repo_root)
    if recomputed is None:
        return f"{d.name}: the diff for {head_oid[:8]} could not be recomputed"
    if recomputed != att.get("diff_sha256"):
        return (
            f"{d.name}: the review covered a different diff than the one being published "
            f"(recorded {str(att.get('diff_sha256'))[:12]}, now {recomputed[:12]}). "
            "Amending, rebasing, or reviewing a narrower base invalidates the evidence."
        )

    if pr_number is not None and att.get("pr_number") not in (pr_number, None):
        return f"{d.name}: reviewed PR #{att.get('pr_number')}, not #{pr_number}"

    if ((reviewers.get("adversarial") or {}).get("verdict") or "").strip() == "needs-attention":
        required = findings_needing_ack(d)
        if required:
            ok, why = _ack_covers(d, required)
            if not ok:
                return f"{d.name}: {why}"
    return None


def verify(repo_root: str | None, head_oid: str, *, pr_number: int | None = None) -> Decision:
    """Whether a completed, diff-bound, acknowledged review covers ``head_oid``."""
    if not head_oid:
        return Decision(False, "the head OID being published could not be resolved")
    if not REPORTS.is_dir():
        return Decision(False, f"no review reports directory at {REPORTS}")

    seen_head = False
    last_reason = ""
    for d in sorted((p for p in REPORTS.iterdir() if p.is_dir()), reverse=True):
        att = _load(d / "ATTESTATION.json")
        if not att or att.get("head_oid") != head_oid:
            continue
        seen_head = True
        rejection = _rejection(att, d, repo_root, head_oid, pr_number)
        if rejection is None:
            verdict = ((att.get("reviewers") or {}).get("adversarial") or {}).get("verdict") or "n/a"
            return Decision(True, f"{head_oid[:8]} reviewed ({d.name}, verdict={verdict})", d)
        last_reason = rejection

    if not seen_head:
        last_reason = (
            f"no completed Codex review covers {head_oid[:8]}.\n"
            "  Run:  /dual-review --base <ref> --cwd <repo>   (or --pr N)\n"
            "  A report stamped REVIEWED_SHA but holding no ATTESTATION.json does NOT count:\n"
            "  that stamp is written when the reviewers are launched, not when they report."
        )
    return Decision(False, last_reason)
