"""Stage-3 cost-profile-ref unit tests.

Verifies:
  1. ``rg -n "cost_model:" research/alphas/`` is empty (regression guard).
  2. Every ``cost_profile_refs`` entry resolves to a key in
     ``config/research/cost_profiles.yaml``.
  3. ``AlphaManifest.from_dict(..., strict=True)`` rejects unknown keys
     while ``strict=False`` (default) accepts them.
  4. The new ``cost_profile_refs`` field round-trips through ``from_dict``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from research.backtest.cost_models import load_cost_profile
from research.registry.schemas import AlphaManifest

REPO_ROOT = Path(__file__).resolve().parents[3]
ALPHAS_DIR = REPO_ROOT / "research" / "alphas"
COST_PROFILES_YAML = REPO_ROOT / "config" / "research" / "cost_profiles.yaml"


def test_no_top_level_cost_model_keys_remain() -> None:
    pattern = re.compile(r"^cost_model:", re.MULTILINE)
    offenders: list[Path] = []
    for manifest in ALPHAS_DIR.rglob("manifest.yaml"):
        if pattern.search(manifest.read_text(encoding="utf-8")):
            offenders.append(manifest)
    assert offenders == [], f"unmigrated cost_model: blocks: {offenders}"


def test_every_cost_profile_ref_resolves() -> None:
    valid_keys = set(yaml.safe_load(COST_PROFILES_YAML.read_text(encoding="utf-8")) or {})
    assert valid_keys, "cost_profiles.yaml is empty?"

    failures: list[tuple[str, list[str]]] = []
    for manifest in ALPHAS_DIR.rglob("manifest.yaml"):
        body = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        refs = body.get("cost_profile_refs") or []
        bad = [r for r in refs if r not in valid_keys]
        if bad:
            failures.append((str(manifest.relative_to(REPO_ROOT)), bad))
    assert failures == [], f"unresolved cost_profile_refs: {failures}"


def test_resolver_returns_cost_model() -> None:
    """Spot check: load_cost_profile must return a usable TAIFEXCost."""
    cost = load_cost_profile("TMFD6")
    # TAIFEXCost is a dataclass with per-side numeric fields.
    assert hasattr(cost, "commission_pts_per_side")
    assert cost.commission_pts_per_side > 0


def test_from_dict_strict_rejects_unknown_top_level_keys() -> None:
    bad = {
        "alpha_id": "test",
        "hypothesis": "h",
        "formula": "f",
        "paper_refs": [],
        "data_fields": [],
        "complexity": "O(1)",
        "completely_made_up_field": 123,
    }
    with pytest.raises(ValueError, match="unknown top-level keys"):
        AlphaManifest.from_dict(bad, strict=True)


def test_from_dict_strict_accepts_known_extras() -> None:
    body = {
        "alpha_id": "test",
        "hypothesis": "h",
        "formula": "f",
        "paper_refs": [],
        "data_fields": [],
        "complexity": "O(1)",
        "cost_profile_notes": {"source_memo": "memo"},
        "expected_edge": {"note": "x"},
        "notes": ["a"],
    }
    m = AlphaManifest.from_dict(body, strict=True)
    assert m.alpha_id == "test"


def test_from_dict_default_is_permissive() -> None:
    """Backwards-compat: strict=False (default) ignores unknown keys."""
    body = {
        "alpha_id": "test",
        "hypothesis": "h",
        "formula": "f",
        "paper_refs": [],
        "data_fields": [],
        "complexity": "O(1)",
        "completely_made_up_field": 123,
    }
    m = AlphaManifest.from_dict(body)  # no strict= → False
    assert m.alpha_id == "test"


def test_cost_profile_refs_field_roundtrip() -> None:
    body = {
        "alpha_id": "test",
        "hypothesis": "h",
        "formula": "f",
        "paper_refs": [],
        "data_fields": [],
        "complexity": "O(1)",
        "cost_profile_refs": ["TXFD6", "TMFD6"],
    }
    m = AlphaManifest.from_dict(body)
    assert m.cost_profile_refs == ("TXFD6", "TMFD6")
