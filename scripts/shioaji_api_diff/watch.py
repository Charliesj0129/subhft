"""Detect Shioaji releases the platform has not yet assessed.

The repo can already *assess* a version (``orchestrate`` captures its surface,
``report`` classifies the diff). What it could not do is notice that a version
exists. ``.github/dependabot.yml`` ignores ``shioaji`` outright, so the one
mechanism that used to announce a release was switched off -- and the ignore is
blunter than its own stated rationale, which is about 1.7.x churn: it also hides
patch releases on the pinned minor line, which are the low-risk fixes the
stabilization charter would actually want.

This module closes that loop. It answers, from PyPI plus the committed
surface goldens: which published releases are newer than the pin, and which of
those has nobody looked at yet.

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
from typing import Any, NamedTuple

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


class Release(NamedTuple):
    """One published version, positioned relative to the pin."""

    version: str
    key: tuple[int, int, int]
    gap: str
    surface_captured: bool

    @property
    def assessed(self) -> bool:
        """Whether the repo holds a captured API surface for this release.

        A captured surface is what makes ``report`` able to say SAFE/BLOCKED, so
        its absence -- not the version being new -- is what needs action.
        """
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


def newer_releases(pin: str, versions: list[str]) -> list[Release]:
    """Published releases strictly newer than ``pin``, most severe gap first."""
    pin_key = parse_version(pin)
    if pin_key is None:
        raise SystemExit(f"pinned version is not a plain release: {pin!r}")
    captured = _captured_versions()
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
            )
        )
    return sorted(found, key=lambda r: (_RANK[r.gap], r.key))


def build_report(pin: str, releases: list[Release]) -> dict[str, Any]:
    unassessed = [r for r in releases if not r.assessed]
    return {
        "pin": pin,
        "generated_by": "scripts/shioaji_api_diff watch",
        "releases": [
            {
                "version": r.version,
                "gap": r.gap,
                "surface_captured": r.surface_captured,
            }
            for r in releases
        ],
        "counts": {
            "newer": len(releases),
            "unassessed": len(unassessed),
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
        mark = "assessed" if row["surface_captured"] else "NOT ASSESSED"
        lines.append(f"  {row['version']:<8} {row['gap']:<6} {mark:<12} ({_GAP_BLURB[row['gap']]})")

    missing = [r["version"] for r in rows if not r["surface_captured"]]
    if missing:
        lines += [
            "",
            "No captured API surface for: " + " ".join(missing),
            "Capture and classify them with:",
            f"  make shioaji-surface VERSIONS='{' '.join(missing)}'",
            f"  uv run python -m scripts.shioaji_api_diff report --pair {report['pin']}:{missing[0]}",
        ]
    return "\n".join(lines) + "\n"
