"""Stage-5 CLI orchestration consolidation (D2) integration tests.

Asserts that ``hft alpha pipeline {run,triage}`` is a faithful delegate of
``python -m research.pipeline {run,triage}``:

  1. ``--help`` for both subcommands exposes the same argparse surface
     (every long-option in `research.pipeline` is also accepted by the new
     CLI).
  2. ``triage`` is gated by ``HFT_RESEARCH_ALLOW_TRIAGE=1`` exactly like the
     legacy entrypoint.
  3. ``Makefile`` `research` / `research-triage` targets now shell out to
     `hft alpha pipeline …` rather than `python -m research.pipeline …`.

The pipeline body itself is exercised by the rest of the test suite (Gate-A
through Gate-F unit + integration tests under tests/unit/alpha/ and
tests/integration/test_alpha_factory_e2e.py); this file only verifies the
CLI plumbing.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _long_opts(help_text: str) -> set[str]:
    return set(re.findall(r"--[a-z0-9][a-z0-9-]*", help_text))


def _help(*argv: str) -> str:
    proc = subprocess.run(
        [sys.executable, *argv, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_pipeline_run_help_surface_matches_legacy_entrypoint() -> None:
    legacy = _long_opts(_help("-m", "research.pipeline", "run"))
    canonical = _long_opts(_help("-m", "hft_platform", "alpha", "pipeline", "run"))
    # Canonical wrapper must accept every long-option the legacy entrypoint did.
    missing = legacy - canonical
    assert not missing, f"hft alpha pipeline run is missing args: {missing}"


def test_pipeline_triage_help_surface_matches_legacy_entrypoint() -> None:
    legacy = _long_opts(_help("-m", "research.pipeline", "triage"))
    canonical = _long_opts(_help("-m", "hft_platform", "alpha", "pipeline", "triage"))
    missing = legacy - canonical
    assert not missing, f"hft alpha pipeline triage is missing args: {missing}"


def test_pipeline_triage_requires_env_acknowledgment() -> None:
    """Without HFT_RESEARCH_ALLOW_TRIAGE=1 the canonical wrapper exits non-zero."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hft_platform",
            "alpha",
            "pipeline",
            "triage",
            "--alpha-id",
            "x",
            "--owner",
            "o",
            "--data",
            "/tmp/x.npy",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "HFT_RESEARCH_ALLOW_TRIAGE"},
        check=False,
    )
    assert proc.returncode != 0
    assert "HFT_RESEARCH_ALLOW_TRIAGE" in (proc.stderr + proc.stdout)


def test_makefile_research_targets_delegate_to_canonical_cli() -> None:
    """`make research` / `make research-triage` must shell out to `hft alpha pipeline …`."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    # Find the two recipe bodies and confirm the dispatch path.
    assert "hft_platform alpha pipeline run" in makefile, (
        "Makefile `research` target no longer delegates to `hft alpha pipeline run`."
    )
    assert "hft_platform alpha pipeline triage" in makefile, (
        "Makefile `research-triage` target no longer delegates to `hft alpha pipeline triage`."
    )
    # Regression guard: the legacy module path must NOT remain in the recipe.
    assert "python -m research.pipeline run" not in makefile
    assert "python -m research.pipeline triage" not in makefile
