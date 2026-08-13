from __future__ import annotations

import ast
from pathlib import Path

from hft_platform.contracts.strategy import StormGuardState as ContractStormGuardState
from hft_platform.risk.storm_guard import StormGuardState as CompatibilityStormGuardState

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_health_reads_storm_guard_state_from_canonical_contract() -> None:
    path = _REPO_ROOT / "src" / "hft_platform" / "observability" / "health.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "hft_platform.contracts.strategy" in imported_modules
    assert "hft_platform.risk.storm_guard" not in imported_modules


def test_storm_guard_compatibility_export_retains_contract_identity() -> None:
    assert CompatibilityStormGuardState is ContractStormGuardState
