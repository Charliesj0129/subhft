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
GATED_PREFIXES = (
    "src/",
    "rust_core/",
    "config/",
    # The gate protects itself. Without these, a change that weakens or deletes
    # the review hooks is "not source" and publishes freely -- after which
    # nothing stops the next one.
    ".claude/hooks/",
    "scripts/git-hooks/",
    # ...and the file that ACTIVATES them. Gating only the implementations left
    # the registrations ungated: a branch deleting the PreToolUse entries touches
    # no gated prefix, publishes freely, and disables enforcement on checkout.
    ".claude/settings.json",
)

#: Severities that must be named in ACK.md before a ``needs-attention`` review can
#: publish. Medium and low are deliberately exempt -- friction disproportionate to
#: severity gets routed around, and a gate nobody uses protects nothing.
ACK_SEVERITIES = ("critical", "high", "p0", "p1")

#: The four bodies ``render.mjs`` emits when the native reviewer produced nothing.
#: ``review.md`` is 95 bytes in that state; a real report is kilobytes.
NATIVE_FAILURES = (
    "Reviewer failed to output a response.",
    "Codex review failed.",
    "Codex review completed without any stdout output.",
    "Codex did not return valid structured JSON.",
)

# The provider aborting the run mid-investigation. Measured over all 28 report
# directories on 2026-08-29: this line appears in 7 of them and in every one of
# those `review.md` is the 95-byte dead body, while all 21 without it hold a real
# report. It is the only signal that separates the two -- and it is the signal
# `Verdict:` alone cannot give, because a truncated adversarial reviewer still
# renders one. Both `approve` verdicts in the entire corpus are runs carrying
# this line; not one reviewer that ran to completion has ever said approve.
PROVIDER_ABORT = "[codex] Codex error:"

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


def canonical_base(head: str, cwd: str | None = None) -> str | None:
    """The base a review of ``head`` is expected to have used."""
    return git("merge-base", "origin/main", head, cwd=cwd) or None


def canonical_range(head: str, cwd: str | None = None) -> str | None:
    """``<merge-base>..<head>``, or None when the range cannot be formed."""
    base = canonical_base(head, cwd=cwd)
    return f"{base}..{head}" if base else None


def diff_sha256(base: str, head: str, cwd: str | None = None) -> str | None:
    """sha256 of the diff from ``base`` to ``head``.

    The base is an explicit argument, not re-derived, because ``origin/main``
    moves: this repo lands a benchmark-baseline commit on every CI run, and a
    merge-base recomputed after that advance would invalidate an otherwise valid
    review. Binding to the recorded base keeps the hash stable; the separate
    coverage check below is what stops a deliberately narrow base from being
    used to shrink what was reviewed.

    ``--no-renames`` matters twice. It makes the hash stable across git versions
    and rename-detection thresholds, and it stops a source file *moved out of* a
    gated directory from disappearing: with rename detection ``--name-only``
    reports only the destination, so moving ``src/x.py`` to ``docs/x.py`` deletes
    production source while showing the gate no ``src/``.
    """
    out = subprocess.run(
        ["git", "diff", "--no-renames", "--full-index", f"{base}..{head}"],
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


def native_state(text: str, err: str = "") -> tuple[bool, str | None]:
    """(completed, failure sentence) for the native reviewer.

    `/codex:review` renders free-form prose and NEVER a `Verdict:` line -- true
    of all 28 historical reports -- so completion has to be read as the absence
    of the four bodies render.mjs writes when nothing came back. `review.md` is
    95 bytes in that state; a real report is kilobytes.

    `err` is the `.err` sidecar. An unreadable one reads as "no error", which is
    the fail-open direction -- but the four bodies above are checked
    independently, so the sidecar is the second lock, never the only one.
    """
    if not text.strip():
        return False, "no output"
    for sentence in NATIVE_FAILURES:
        if sentence in text:
            return False, sentence
    if PROVIDER_ABORT in err:
        return False, _abort_reason(err)
    return True, None


def adversarial_state(text: str, err: str = "", status: str = "") -> tuple[bool, str | None]:
    """(completed, verdict) for the adversarial reviewer.

    `/codex:adversarial-review` is schema-constrained
    (schemas/review-output.schema.json, verdict enum approve|needs-attention), so
    a `Verdict:` line means the JSON parsed. It does NOT mean the reviewer
    finished looking: a run the provider aborts renders a verdict over whatever
    it had reached. On 2026-08-29 that produced `Verdict: approve` /
    "No material findings" from a 375-byte report whose sidecar says the usage
    limit was hit -- an approval of a branch the reviewer had not finished
    reading, which is exactly the empty-review-reads-like-an-approval failure
    this whole mechanism exists to close, arriving through the mechanism itself.

    So the verdict is necessary and not sufficient. Neither is the sidecar:
    absence of a known error string is not evidence of success, and building the
    whole discriminator on a negative meant any UNKNOWN failure -- a new provider
    message, a killed process, a lost sidecar -- read as "finished". `status` is
    the positive half: the launcher records the reviewer's exit code only after
    it exits, so a missing, unreadable, or non-zero status is incomplete.

    `Turn failed.` on its own stays non-authoritative -- it is coextensive with
    PROVIDER_ABORT across all 28 reports and adds nothing.
    """
    m = _VERDICT.search(text)
    if not m:
        return False, None
    if status.strip() != "0":
        return False, m.group(1)
    if PROVIDER_ABORT in err:
        return False, m.group(1)
    return True, m.group(1)


def _abort_reason(err: str) -> str:
    """The provider's own abort line, trimmed to one sentence for the report."""
    for line in err.splitlines():
        if line.startswith(PROVIDER_ABORT):
            return line[len("[codex] ") :].strip()[:160]
    return "provider aborted the run"


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
    *,
    repo_root: str,
    mode: str,
    pr_number: int | None,
    native: str,
    adversarial: str,
    base: str | None,
    head: str,
    now: str,
    native_err: str = "",
    adversarial_err: str = "",
    adversarial_status: str = "",
) -> dict:
    """The attestation payload. Pure apart from the git calls it needs for the hash.

    Lives here, not in the launcher, because the launcher sits outside this
    repository and outside its CI. The consumer must never be the only tested
    half of a contract.
    """
    native_ok, native_failure = native_state(native, native_err)
    adv_ok, verdict = adversarial_state(adversarial, adversarial_err, adversarial_status)
    sha = None if (mode == "working-tree" or not base or not head) else diff_sha256(base, head, cwd=repo_root)
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
            "adversarial": {
                "completed": adv_ok,
                "verdict": verdict,
                "failure": None if adv_ok else (_abort_reason(adversarial_err) if verdict else "no verdict"),
            },
        },
        "findings": all_findings(native, adversarial),
        # A working-tree review can never credit a publish, so it is never
        # complete for the gate's purposes even when both reviewers reported.
        # Only the adversarial reviewer is REQUIRED. Measured over 30 report
        # directories on 2026-08-29: the native reviewer died 13 times (43%) to
        # the adversarial one's 1, and produced a `Verdict:` line, a severity,
        # or a file:line anchor in exactly none of them -- there is nothing in
        # its output a gate can read. It still runs when asked and its failure
        # is still recorded; it just no longer decides whether work can ship.
        # A required check that fails two times in five gets switched off, and
        # then nothing is checked at all.
        "complete": bool(adv_ok and mode != "working-tree" and sha),
    }


def _is_ancestor(maybe_ancestor: str, descendant: str, cwd: str | None = None) -> bool:
    out = subprocess.run(
        ["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
        cwd=cwd,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return out.returncode == 0


def blocking_findings(att: dict) -> list[tuple[str, str]]:
    """(severity, title) for every finding in the attestation that needs an ACK."""
    out: list[tuple[str, str]] = []
    for f in att.get("findings") or []:
        sev = str(f.get("severity", "")).lower()
        if sev in ACK_SEVERITIES:
            out.append((sev, str(f.get("title", ""))))
    return out


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
            if sev.lower() in ACK_SEVERITIES:
                out.append((sev.upper(), title))
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


def _rejection(
    att: dict, d: Path, repo_root: str | None, head_oid: str, pr_number: int | None
) -> tuple[str | None, bool]:
    """(why this attestation cannot credit the publish, is that answer final).

    Split out of ``verify`` so each rule reads as one clause. Order matters: the
    cheap structural checks run before the diff is recomputed.

    The second element separates "this report does not apply to what is being
    published" -- unfinished, stale hash, wrong PR, narrower base -- from "this
    report applies to exactly this diff and says NO". Only the second is final.
    Without that distinction ``verify`` fell past a fresh blocking review to an
    older approval of the same head, so re-reviewing and FINDING a defect did
    not revoke the earlier approval.
    """
    if not att.get("complete"):
        why = att.get("failure")
        # The producer records WHY when it refuses to build a real attestation
        # (e.g. the verifier is not readable from origin/main). Printing only
        # "never finished" for that case sends the reader to the reviewer logs,
        # where there is nothing wrong to find.
        return (f"{d.name}: {why}" if why else f"{d.name}: the review never finished (complete=false)"), False
    if att.get("mode") == "working-tree":
        return (
            f"{d.name}: reviewed the working tree, not commits. Uncommitted files are not "
            "what a push publishes -- re-review the branch."
        ), False

    reviewers = att.get("reviewers") or {}
    if not (reviewers.get("adversarial") or {}).get("completed"):
        return (
            f"{d.name}: the adversarial reviewer produced no report -- a launched review is not a completed one."
        ), False

    reviewed_base = att.get("base_oid")
    if not reviewed_base:
        return f"{d.name}: the attestation records no reviewed base", False

    recomputed = diff_sha256(reviewed_base, head_oid, cwd=repo_root)
    if recomputed is None:
        return f"{d.name}: the diff for {head_oid[:8]} could not be recomputed", False
    if recomputed != att.get("diff_sha256"):
        return (
            f"{d.name}: the review covered a different diff than the one being published "
            f"(recorded {str(att.get('diff_sha256'))[:12]}, now {recomputed[:12]}). "
            "Amending or rebasing after the review invalidates the evidence."
        ), False

    # A hash bound to the recorded base is stable while origin/main advances, but
    # on its own it would let `--base HEAD^` shrink what "reviewed" means.
    #
    # Comparing the two sets of gated FILENAMES is not enough, and the reviewers
    # reproduced why: when an unreviewed commit and a reviewed commit touch the
    # same file, the sets are equal and the narrow attestation passes with the
    # earlier hunks never read. Ancestry is the exact test -- the reviewed base
    # must be at or before the canonical one, so the reviewed range is a superset.
    canonical = canonical_base(head_oid, cwd=repo_root)
    if not canonical:
        return f"{d.name}: no merge-base with origin/main for {head_oid[:8]}", False
    if reviewed_base != canonical and not _is_ancestor(reviewed_base, canonical, cwd=repo_root):
        return (
            f"{d.name}: the review base ({str(reviewed_base)[:8]}) is NARROWER than the range being "
            f"published (canonical base {canonical[:8]}), so commits before it were never reviewed. "
            "Re-review without a hand-picked --base."
        ), False

    if pr_number is not None and att.get("pr_number") not in (pr_number, None):
        return f"{d.name}: reviewed PR #{att.get('pr_number')}, not #{pr_number}", False

    # Blocking findings come from the attestation, which is written once and never
    # edited, not from the report markdown. Deleting adversarial.md used to empty
    # the requirement list and turn a NO SHIP into an approval; and a native P1
    # went unacknowledged whenever the adversarial reviewer happened to approve,
    # so the requirement is per-finding rather than per-verdict.
    required = blocking_findings(att) or findings_needing_ack(d)
    if required:
        ok, why = _ack_covers(d, required)
        if not ok:
            # FINAL: this review read exactly this diff and named a blocking
            # finding. An older approval of the same head cannot outvote it.
            return f"{d.name}: {why}", True
    return None, False


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
        rejection, final = _rejection(att, d, repo_root, head_oid, pr_number)
        if rejection is None:
            verdict = ((att.get("reviewers") or {}).get("adversarial") or {}).get("verdict") or "n/a"
            return Decision(True, f"{head_oid[:8]} reviewed ({d.name}, verdict={verdict})", d)
        last_reason = rejection
        if final:
            # Newest first, so this is the most recent review that actually read
            # this diff. Falling through to an older approval would mean a
            # re-review that FOUND something could not revoke the first one.
            return Decision(False, rejection)

    if not seen_head:
        last_reason = (
            f"no completed Codex review covers {head_oid[:8]}.\n"
            "  Run:  /dual-review --base <ref> --cwd <repo>   (or --pr N)\n"
            "  A report stamped REVIEWED_SHA but holding no ATTESTATION.json does NOT count:\n"
            "  that stamp is written when the reviewers are launched, not when they report."
        )
    return Decision(False, last_reason)
