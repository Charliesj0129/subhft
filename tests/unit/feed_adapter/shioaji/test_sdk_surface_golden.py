"""Regression guard: the installed shioaji API surface must match its golden.

This fails CI when a dependabot (or manual) shioaji bump silently changes the
SDK surface the platform depends on, with a readable diff and regeneration
instructions. It introspects the ALREADY-INSTALLED package only — no network,
no broker connection — so it is safe inside the standard unit-test job.

When the bump is intentional: run ``make shioaji-surface-regen``, review the
diff against the prior version (``make shioaji-diff``), and commit the new
``surface_<version>.json``.
"""

from __future__ import annotations

import copy
import difflib
import json
from typing import Any

import pytest

shioaji = pytest.importorskip("shioaji")

from scripts.shioaji_api_diff._capture_entrypoint import (  # noqa: E402
    build_surface_snapshot,
    canonical_json,
)
from scripts.shioaji_api_diff.paths import GOLDEN_DIR  # noqa: E402


def _strip_volatile(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Drop environment-dependent fields before comparison.

    ``nm_symbols`` depend on the local ``nm``/toolchain and ``snapshot_sha256``
    is derived from the body — neither reflects an SDK API change.

    ``capture.pydantic_runtime`` records the *ambient* pydantic of whatever venv
    did the capture, not anything about shioaji's surface. The committed golden
    is produced by the orchestrator in a throwaway venv that installs shioaji
    ALONE; shioaji >=1.5.x declares almost no deps (the Rust core bundles them),
    so pydantic is absent there and the field reads ``"none"``. This guard,
    however, runs inside the full platform venv (pydantic 2 present), so it would
    read ``"2"`` and spuriously fail an otherwise byte-identical surface. Strip
    it so the guard compares the SDK surface, not the capturing environment.
    (The cross-version orchestrator diff intentionally keeps this field — 1.5.x
    dropping the pydantic dependency is a real, reportable version delta there.)
    """
    snap = copy.deepcopy(snapshot)
    snap.pop("snapshot_sha256", None)
    if isinstance(snap.get("capture"), dict):
        snap["capture"].pop("pydantic_runtime", None)
    for entry in (snap.get("compiled") or {}).values():
        if isinstance(entry, dict):
            entry.pop("nm_symbols", None)
    return snap


@pytest.mark.unit
def test_installed_shioaji_surface_matches_committed_golden() -> None:
    version = shioaji.__version__
    golden = GOLDEN_DIR / f"surface_{version}.json"
    if not golden.exists():
        pytest.fail(
            f"No committed surface golden for shioaji {version}. A version bump "
            f"landed without a snapshot.\n"
            f"Regenerate:  make shioaji-surface-regen\n"
            f"Then review `make shioaji-diff` and commit "
            f"tests/golden/shioaji_sdk/surface_{version}.json."
        )
    expected = _strip_volatile(json.loads(golden.read_text(encoding="utf-8")))
    actual = _strip_volatile(build_surface_snapshot())
    if actual != expected:
        delta = "\n".join(
            difflib.unified_diff(
                canonical_json(expected).splitlines(),
                canonical_json(actual).splitlines(),
                fromfile=f"golden surface_{version}.json",
                tofile="installed shioaji surface",
                lineterm="",
            )
        )
        pytest.fail(
            f"Installed shioaji {version} API surface drifted from the committed "
            f"golden. If this bump is intentional, run `make shioaji-surface-regen`, "
            f"review `make shioaji-diff`, update docs/runbooks/shioaji-version-diff.md, "
            f"and commit.\n\n{delta[:6000]}"
        )


@pytest.mark.unit
def test_capture_is_deterministic() -> None:
    """Two captures of the same install must be byte-identical."""
    first = canonical_json(build_surface_snapshot())
    second = canonical_json(build_surface_snapshot())
    assert first == second


@pytest.mark.unit
def test_strip_volatile_ignores_ambient_pydantic_runtime() -> None:
    """The golden guard must not fail just because the capturing venv has a
    different ambient pydantic than the minimal venv that produced the golden.

    Regression for the shioaji 1.5.3 case: the committed surface_1.5.3.json was
    captured with pydantic absent (``"none"``), but this guard runs in the full
    platform venv (``"2"``). Both must compare equal after stripping.
    """
    golden_like = {"capture": {"pydantic_runtime": "none", "python": "3.12"}, "constants": {"A": 1}}
    installed_like = {"capture": {"pydantic_runtime": "2", "python": "3.12"}, "constants": {"A": 1}}
    assert _strip_volatile(golden_like) == _strip_volatile(installed_like)

    # A REAL surface change (a constant differs) must still be detected.
    drifted = {"capture": {"pydantic_runtime": "2", "python": "3.12"}, "constants": {"A": 2}}
    assert _strip_volatile(golden_like) != _strip_volatile(drifted)
