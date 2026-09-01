"""Detect Shioaji releases the platform has not yet assessed.

The repo can already *assess* a version (``orchestrate`` captures its surface,
``report`` classifies the diff). What it could not do is notice that a version
exists. ``.github/dependabot.yml`` ignores ``shioaji`` outright, so the one
mechanism that used to announce a release was switched off -- and the ignore is
blunter than its own stated rationale, which is about 1.7.x churn: it also hides
patch releases on the pinned minor line, which are the low-risk fixes the
stabilization charter would actually want.

This module closes that loop. It answers, from PyPI plus the committed surface
goldens plus the decision ledger: which published releases are newer than the
pin, and which of those is still waiting on someone.

"Waiting on someone" is deliberately three things, not one. A captured API
surface is evidence; it is not a decision, and treating it as one made "run the
capture command" the way to silence a release nobody had thought about. So a
release is settled only when its surface is captured, a verdict is recorded in
``docs/runbooks/shioaji-release-decisions.yaml``, and that verdict has not
expired -- a DEFER carries a revisit date precisely so that "not now" comes
back on its own.

Deliberately stdlib-only and side-effect-free -- it reads ``pyproject.toml`` and
``tests/golden/shioaji_sdk/``, never writes them, and never touches the
project venv. ``--releases-json`` substitutes a local file for the PyPI call so
the classification is testable without a network.
"""

from __future__ import annotations

import json
import re
import tomllib
import urllib.request
from collections.abc import Mapping
from datetime import date
from typing import Any, NamedTuple, Protocol

from .paths import GOLDEN_DIR, REPO_ROOT

PYPI_URL = "https://pypi.org/pypi/shioaji/json"
_HTTP_TIMEOUT_S = 20

# Releases are only comparable when they are plain ``X.Y.Z``. Shioaji's history
# also contains ``0.3.6.dev4``-style pre-releases; those are never upgrade
# candidates for a production pin, so they are dropped rather than ordered.
_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# What the gap between a candidate and the pin means for the upgrade decision.
PATCH = "PATCH"  # same major.minor as the pin -- fixes only, cheapest to adopt
MINOR = "MINOR"  # a new feature line -- needs a full surface diff first
MAJOR = "MAJOR"  # a new major line

# Ordered by how adoptable the gap is, not by how severe it is. This report
# exists to answer "what should move next", and the answer is nearly always the
# patch on the pinned line: it carries fixes only, needs no adapter work, and is
# the one row a stabilization freeze is meant to still allow through. Sorting by
# severity buries it under a feature line nobody is going to take this quarter.
_RANK = {PATCH: 0, MINOR: 1, MAJOR: 2}


# Why a release still needs someone's attention. Ordered by what to do about it.
NO_SURFACE = "NO_SURFACE"  # nobody has captured its API surface -- no evidence yet
UNDECIDED = "UNDECIDED"  # evidence exists, but nothing was ever decided
EXPIRED = "EXPIRED"  # a deferral came due
SETTLED = "SETTLED"  # captured, decided, and the decision still stands


class DecisionLike(Protocol):
    """The shape ``decisions.Decision`` presents to this module.

    Structural rather than imported: the ledger is YAML and this module is
    deliberately stdlib-only, so that a scheduled release check keeps working
    even where the project venv does not.
    """

    @property
    def verdict(self) -> str: ...

    def status(self, today: date) -> str: ...


class Release(NamedTuple):
    """One published version, positioned relative to the pin."""

    version: str
    key: tuple[int, int, int]
    gap: str
    surface_captured: bool
    decision: DecisionLike | None = None

    def state(self, today: date) -> str:
        """What, if anything, this release is still waiting on.

        A captured surface used to be the whole test, which made "run the
        capture command" the way to silence a release nobody had thought about.
        The surface is evidence; the ledger entry is the decision; and a
        deferral that has come due is neither.
        """
        if not self.surface_captured:
            return NO_SURFACE
        if self.decision is None:
            return UNDECIDED
        return EXPIRED if self.decision.status(today) == "EXPIRED" else SETTLED

    @property
    def assessed(self) -> bool:
        """Whether the repo holds a captured API surface for this release."""
        return self.surface_captured


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` for a plain release, or ``None`` for anything else."""
    match = _RELEASE_RE.match(text.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def read_pin(pyproject: Any = None) -> str:
    """The ``shioaji`` version ``pyproject.toml`` pins.

    Parsed with ``tomllib`` rather than grepped: the pin is the fact the whole
    report is relative to, so reading it wrongly would misclassify every row.
    """
    path = REPO_ROOT / "pyproject.toml" if pyproject is None else pyproject
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    for spec in (data.get("project") or {}).get("dependencies") or []:
        name, _, requirement = str(spec).partition("==")
        # ``shioaji[speed]`` -- the extras are part of the name, not the version.
        if name.split("[")[0].strip() == "shioaji" and requirement:
            return requirement.strip()
    raise SystemExit("no pinned shioaji requirement found in pyproject.toml [project.dependencies]")


def fetch_releases(url: str = PYPI_URL) -> list[str]:
    """Every non-yanked version string PyPI lists for shioaji."""
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_S) as response:  # noqa: S310 - fixed https URL
        payload = json.load(response)
    return releases_from_payload(payload)


def releases_from_payload(payload: dict[str, Any]) -> list[str]:
    """Non-yanked, installable version strings from a PyPI JSON payload."""
    out: list[str] = []
    for version, files in (payload.get("releases") or {}).items():
        # A version with no files, or whose every file is yanked, is not
        # installable -- offering it as a candidate would waste a capture run.
        usable = [f for f in files or [] if not f.get("yanked")]
        if usable:
            out.append(version)
    return out


def _captured_versions() -> set[str]:
    return {path.name[len("surface_") : -len(".json")] for path in GOLDEN_DIR.glob("surface_*.json")}


def _gap(pin: tuple[int, int, int], candidate: tuple[int, int, int]) -> str:
    if candidate[0] != pin[0]:
        return MAJOR
    if candidate[1] != pin[1]:
        return MINOR
    return PATCH


def newer_releases(
    pin: str,
    versions: list[str],
    decisions: Mapping[str, DecisionLike] | None = None,
) -> list[Release]:
    """Published releases strictly newer than ``pin``, most adoptable gap first."""
    pin_key = parse_version(pin)
    if pin_key is None:
        raise SystemExit(f"pinned version is not a plain release: {pin!r}")
    captured = _captured_versions()
    ledger = decisions or {}
    found: list[Release] = []
    for version in versions:
        key = parse_version(version)
        if key is None or key <= pin_key:
            continue
        found.append(
            Release(
                version=version,
                key=key,
                gap=_gap(pin_key, key),
                surface_captured=version in captured,
                decision=ledger.get(version),
            )
        )
    return sorted(found, key=lambda r: (_RANK[r.gap], r.key))


def build_report(
    pin: str,
    releases: list[Release],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """The machine report. ``today`` is injectable so deferral expiry is testable."""
    now = today or date.today()
    states = {r.version: r.state(now) for r in releases}
    return {
        "pin": pin,
        "generated_by": "scripts/shioaji_api_diff watch",
        "releases": [
            {
                "version": r.version,
                "gap": r.gap,
                "surface_captured": r.surface_captured,
                "state": states[r.version],
                "verdict": r.decision.verdict if r.decision is not None else None,
            }
            for r in releases
        ],
        "counts": {
            "newer": len(releases),
            "unassessed": sum(1 for r in releases if not r.surface_captured),
            "undecided": sum(1 for v in states.values() if v == UNDECIDED),
            "expired": sum(1 for v in states.values() if v == EXPIRED),
            "needs_action": sum(1 for v in states.values() if v != SETTLED),
            "patch_on_pin_line": sum(1 for r in releases if r.gap == PATCH),
        },
    }


_GAP_BLURB = {
    PATCH: "patch on the pinned line -- fixes only",
    MINOR: "new feature line",
    MAJOR: "new major line",
}


def render_text(report: dict[str, Any]) -> str:
    """Human summary. Names the next command rather than only the problem."""
    lines = [f"pinned: shioaji=={report['pin']}"]
    rows = report["releases"]
    if not rows:
        lines.append("no newer release on PyPI.")
        return "\n".join(lines) + "\n"

    lines.append(f"{len(rows)} newer release(s) on PyPI (most adoptable first):")
    for row in rows:
        verdict = row["verdict"] or "-"
        lines.append(
            f"  {row['version']:<8} {row['gap']:<6} {verdict:<8} {row['state']:<10} ({_GAP_BLURB[row['gap']]})"
        )

    missing = [r["version"] for r in rows if r["state"] == NO_SURFACE]
    if missing:
        lines += [
            "",
            "No captured API surface for: " + " ".join(missing),
            "Capture and classify them with:",
            f"  make shioaji-surface VERSIONS='{' '.join(missing)}'",
            f"  uv run python -m scripts.shioaji_api_diff report --pair {report['pin']}:{missing[0]}",
        ]

    undecided = [r["version"] for r in rows if r["state"] == UNDECIDED]
    expired = [r["version"] for r in rows if r["state"] == EXPIRED]
    if undecided or expired:
        lines.append("")
        if undecided:
            lines.append("No recorded decision for: " + " ".join(undecided))
        if expired:
            lines.append("Deferral came due for: " + " ".join(expired))
        lines += [
            "Record or renew the verdict in:",
            "  docs/runbooks/shioaji-release-decisions.yaml",
        ]
    return "\n".join(lines) + "\n"
