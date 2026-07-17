"""Regression guard: the `hft` CLI must build without the dev-only research/ tree.

Production containers mount ``src/`` but never ``research/``; before the
2026-07-17 fix, ``build_parser()`` imported ``research.pipeline``
unconditionally, so EVERY in-container CLI invocation (including
``hft ops rearm-platform``, which the boot-latch runbook depends on) died
with ModuleNotFoundError.
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest


@pytest.mark.unit
def test_build_parser_succeeds_when_research_package_missing() -> None:
    from hft_platform.cli import _parser as parser_mod

    # Simulate the production container: `import research.pipeline` raises.
    with mock.patch.dict(sys.modules, {"research": None, "research.pipeline": None}):
        parser = parser_mod.build_parser()

    # Core ops surface must survive; the research-only pipeline commands may
    # be absent, but parsing an ops command must not require research/.
    args = parser.parse_args(["ops", "rearm-platform"])
    assert args.ops_cmd == "rearm-platform"


@pytest.mark.unit
def test_build_parser_registers_alpha_pipeline_when_research_present() -> None:
    pytest.importorskip("research.pipeline")
    from hft_platform.cli import _parser as parser_mod

    parser = parser_mod.build_parser()
    args = parser.parse_args(
        ["alpha", "pipeline", "triage", "--alpha-id", "x", "--owner", "y", "--data", "z"]
    )
    assert args.alpha_pipeline_cmd == "triage"
