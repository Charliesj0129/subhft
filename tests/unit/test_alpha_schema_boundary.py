"""Regression checks for canonical alpha-schema ownership and compatibility."""

from __future__ import annotations

import ast
import base64
import pickle
from pathlib import Path

import hft_platform.contracts.alpha as canonical
import research.registry.schemas as legacy

_PUBLIC_CONTRACTS = (
    "AlphaManifest",
    "AlphaProtocol",
    "AlphaStatus",
    "AlphaTier",
    "BatchAlphaProtocol",
    "DEFAULT_LATENCY_PROFILE",
    "EDGE_METRIC_SEMANTICS_SCHEMA",
    "EDGE_METRIC_SOURCE_GATE",
    "EDGE_METRIC_SUPPORTING_GATES",
    "Scorecard",
    "VALID_ROLES",
    "VALID_SKILLS",
    "edge_metric_semantics",
)

_PRODUCTION_CONSUMERS = (
    "src/hft_platform/alpha/_gate_a.py",
    "src/hft_platform/alpha/dsl/formula_context.py",
    "src/hft_platform/alpha/experiments.py",
    "src/hft_platform/alpha/kill_ledger.py",
    "src/hft_platform/alpha/promotion.py",
)

# Captured before the ownership migration from real protocol-4 pickles whose
# globals resolve through research.registry.schemas.
_LEGACY_MANIFEST_PICKLE = (
    "gASVzQEAAAAAAACMGXJlc2VhcmNoLnJlZ2lzdHJ5LnNjaGVtYXOUjA1BbHBoYU1hbmlm"
    "ZXN0lJOUKYGUfZQojAhhbHBoYV9pZJSMDWxlZ2FjeS1waWNrbGWUjApoeXBvdGhlc2lz"
    "lIwBaJSMB2Zvcm11bGGUjAFmlIwKcGFwZXJfcmVmc5SMAXCUhZSMC2RhdGFfZmllbGRz"
    "lIwBeJSFlIwKY29tcGxleGl0eZSMBE8oMSmUjAZzdGF0dXOUaACMC0FscGhhU3RhdHVz"
    "lJOUjAZHQVRFX0GUhZRSlIwEdGllcpRoAIwJQWxwaGFUaWVylJOUjAZUSUVSXzKUhZRS"
    "lIwLcnVzdF9tb2R1bGWUTowPbGF0ZW5jeV9wcm9maWxllIwTc2ltX3A5NV92MjAyNi0w"
    "Mi0yNpSMCnJvbGVzX3VzZWSUjAdwbGFubmVylIWUjAtza2lsbHNfdXNlZJQpjBNmZWF0"
    "dXJlX3NldF92ZXJzaW9ulE6MDXN0cmF0ZWd5X3R5cGWUjAV0YWtlcpSMCmluc3RydW1l"
    "bnSUjACUjAtkc2xfZm9ybXVsYZROjA9wYXJlbnRfYWxwaGFfaWSUTowRY29zdF9wcm9m"
    "aWxlX3JlZnOUKXViLg=="
)

_LEGACY_SCORECARD_PICKLE = (
    "gASVCAIAAAAAAACMGXJlc2VhcmNoLnJlZ2lzdHJ5LnNjaGVtYXOUjAlTY29yZWNhcmSU"
    "k5QpgZR9lCiMCXNoYXJwZV9pc5RHP/QAAAAAAACMCnNoYXJwZV9vb3OUTowHaWNfbWVh"
    "bpROjAZpY19zdGSUTowIdHVybm92ZXKUTowMbWF4X2RyYXdkb3dulE6MFGNvcnJlbGF0"
    "aW9uX3Bvb2xfbWF4lE6MDXJlZ2ltZV9zaGFycGWUfZSMEWNhcGFjaXR5X2VzdGltYXRl"
    "lE6MD2xhdGVuY3lfcHJvZmlsZZROjBh3YWxrX2ZvcndhcmRfc2hhcnBlX21lYW6UTowX"
    "d2Fsa19mb3J3YXJkX3NoYXJwZV9zdGSUTowXd2Fsa19mb3J3YXJkX3NoYXJwZV9taW6U"
    "Towcd2Fsa19mb3J3YXJkX2NvbnNpc3RlbmN5X3BjdJROjBJzdGF0X2JoX25fc3Vydml2"
    "ZWSUTowOc3RhdF9iaF9tZXRob2SUTowPc3RhdF9iZHNfcHZhbHVllE6MFmNvc3Rfc2Vu"
    "c2l0aXZpdHlfcmF0aW+UTowQZGF0YV9maW5nZXJwcmludJROjAhybmdfc2VlZJRLB4wQ"
    "Z2VuZXJhdG9yX3NjcmlwdJROjAdkYXRhX3VslE6MCXJlZ2ltZV9pY5R9lIwVZWRnZV9t"
    "ZXRyaWNfc2VtYW50aWNzlH2UdWIu"
)


def test_legacy_module_reexports_canonical_contract_identities() -> None:
    assert canonical.__all__ == legacy.__all__ == _PUBLIC_CONTRACTS
    for name in _PUBLIC_CONTRACTS:
        assert getattr(legacy, name) is getattr(canonical, name)


def test_alpha_manifest_serialization_contract_is_unchanged() -> None:
    manifest = canonical.AlphaManifest(
        alpha_id="stage6-schema-baseline",
        hypothesis="ownership migration preserves schema behavior",
        formula="bid - ask",
        paper_refs=("paper:1",),
        data_fields=("bid", "ask"),
        complexity="O(1)",
        status=canonical.AlphaStatus.GATE_A,
        tier=canonical.AlphaTier.TIER_2,
        roles_used=("planner",),
        skills_used=("iterative-retrieval",),
        instrument="TXFD6",
        cost_profile_refs=("TXFD6",),
    )

    expected = {
        "alpha_id": "stage6-schema-baseline",
        "hypothesis": "ownership migration preserves schema behavior",
        "formula": "bid - ask",
        "paper_refs": ("paper:1",),
        "data_fields": ("bid", "ask"),
        "complexity": "O(1)",
        "status": "GATE_A",
        "tier": "TIER_2",
        "rust_module": None,
        "latency_profile": "sim_p95_v2026-02-26",
        "roles_used": ("planner",),
        "skills_used": ("iterative-retrieval",),
        "feature_set_version": None,
        "strategy_type": "taker",
        "instrument": "TXFD6",
        "dsl_formula": None,
        "parent_alpha_id": None,
        "cost_profile_refs": ("TXFD6",),
    }
    assert manifest.to_dict() == expected
    assert canonical.AlphaManifest.from_dict(expected, strict=True) == manifest


def test_scorecard_serialization_contract_is_unchanged() -> None:
    scorecard = canonical.Scorecard(
        sharpe_is=1.25,
        rng_seed=7,
        regime_ic={"open": 0.2},
    )

    assert canonical.Scorecard.from_dict(scorecard.to_dict()) == scorecard


def test_historical_pickles_resolve_through_legacy_shim() -> None:
    manifest = pickle.loads(base64.b64decode(_LEGACY_MANIFEST_PICKLE))
    scorecard = pickle.loads(base64.b64decode(_LEGACY_SCORECARD_PICKLE))

    assert type(manifest) is canonical.AlphaManifest
    assert manifest.alpha_id == "legacy-pickle"
    assert manifest.status is canonical.AlphaStatus.GATE_A
    assert manifest.tier is canonical.AlphaTier.TIER_2
    assert type(scorecard) is canonical.Scorecard
    assert scorecard.sharpe_is == 1.25
    assert scorecard.rng_seed == 7


def test_production_alpha_consumers_do_not_import_legacy_research_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    legacy_schema_imports: list[tuple[str, str]] = []

    for relative_path in _PRODUCTION_CONSUMERS:
        tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                legacy_schema_imports.extend(
                    (relative_path, alias.name)
                    for alias in node.names
                    if alias.name == "research.registry.schemas" or alias.name.startswith("research.registry.schemas.")
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "research.registry.schemas" or module.startswith("research.registry.schemas."):
                    legacy_schema_imports.append((relative_path, module))

    assert legacy_schema_imports == []
