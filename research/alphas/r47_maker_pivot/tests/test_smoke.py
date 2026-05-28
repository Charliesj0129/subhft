"""Smoke test stub for r47_maker_pivot.

Real coverage lives under `tests/unit/research/alphas/r47_maker_pivot/` (see project
testing rules at `.agent/rules/50-testing.md`). This stub exists to satisfy
the factory artifact-contract audit; it asserts that the alpha manifest is
present and loadable.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def test_manifest_is_loadable() -> None:
    here = Path(__file__).resolve().parent.parent
    manifest_path = here / "manifest.yaml"
    assert manifest_path.exists(), f"manifest.yaml not found at {manifest_path}"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("alpha_id"), "alpha_id missing"
