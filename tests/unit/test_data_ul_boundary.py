"""Regression checks for canonical DataUL ownership and compatibility."""

from __future__ import annotations

import ast
import base64
import inspect
import pickle
from pathlib import Path

import hft_platform.contracts.data_ul as canonical
import research.tools.vm_ul as legacy

_PUBLIC_CONTRACTS = (
    "DataUL",
    "UL_REQUIRED_FIELDS",
    "coerce_data_ul",
    "infer_data_ul",
    "required_fields_for_ul",
    "validate_meta_ul",
)

# Captured before the ownership migration from a real protocol-4 pickle whose
# global resolves through research.tools.vm_ul.DataUL.
_LEGACY_DATA_UL_PICKLE = "gASVKQAAAAAAAACMFHJlc2VhcmNoLnRvb2xzLnZtX3VslIwGRGF0YVVMlJOUSwOFlFKULg=="


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_legacy_vm_ul_reexports_canonical_contract_identities() -> None:
    assert tuple(canonical.__all__) == tuple(legacy.__all__) == _PUBLIC_CONTRACTS
    for name in _PUBLIC_CONTRACTS:
        assert getattr(legacy, name) is getattr(canonical, name)


def test_canonical_data_ul_public_abi_is_preserved() -> None:
    assert [(member.name, member.value) for member in canonical.DataUL] == [
        ("UL1", 1),
        ("UL2", 2),
        ("UL3", 3),
        ("UL4", 4),
        ("UL5", 5),
        ("UL6", 6),
    ]
    assert str(inspect.signature(canonical.coerce_data_ul)) == (
        "(value: 'Any', default: 'DataUL' = <DataUL.UL2: 2>) -> 'DataUL'"
    )
    assert str(inspect.signature(canonical.required_fields_for_ul)) == ("(min_ul: 'DataUL') -> 'frozenset[str]'")
    assert str(inspect.signature(canonical.validate_meta_ul)) == (
        "(meta: 'Mapping[str, Any]', min_ul: 'DataUL') -> 'tuple[bool, list[str]]'"
    )
    assert str(inspect.signature(canonical.infer_data_ul)) == ("(meta: 'Mapping[str, Any]') -> 'DataUL'")


def test_historical_research_data_ul_pickle_resolves_to_canonical_enum() -> None:
    restored = pickle.loads(base64.b64decode(_LEGACY_DATA_UL_PICKLE))

    assert restored is canonical.DataUL.UL3
    assert type(restored) is canonical.DataUL


def test_coerce_data_ul_preserves_tokens_defaults_and_existing_members() -> None:
    assert canonical.coerce_data_ul(" ul4 ") is canonical.DataUL.UL4
    assert canonical.coerce_data_ul("5") is canonical.DataUL.UL5
    assert canonical.coerce_data_ul(canonical.DataUL.UL6) is canonical.DataUL.UL6
    assert canonical.coerce_data_ul(None) is canonical.DataUL.UL2
    assert canonical.coerce_data_ul("invalid", canonical.DataUL.UL1) is canonical.DataUL.UL1


def test_validate_meta_ul_treats_blank_and_empty_required_values_as_missing() -> None:
    meta = {
        "dataset_id": " ",
        "source_type": "synthetic",
        "schema_version": 1,
        "rows": 100,
        "fields": [],
    }

    ok, missing = canonical.validate_meta_ul(meta, canonical.DataUL.UL2)

    assert ok is False
    assert missing == ["dataset_id", "fields"]


def test_data_ul_contract_does_not_import_runtime_or_research() -> None:
    root = Path(__file__).resolve().parents[2]
    modules = _imported_modules(root / "src/hft_platform/contracts/data_ul.py")

    assert all(not module.startswith("research") for module in modules)
    assert all(
        not module.startswith(
            (
                "hft_platform.alpha",
                "hft_platform.backtest",
                "hft_platform.services",
            )
        )
        for module in modules
    )


def test_production_sources_do_not_import_legacy_vm_ul() -> None:
    root = Path(__file__).resolve().parents[2]
    legacy_imports: list[tuple[str, str]] = []

    for path in (root / "src/hft_platform").rglob("*.py"):
        relative_path = str(path.relative_to(root))
        for module in _imported_modules(path):
            if module == "research.tools.vm_ul" or module.startswith("research.tools.vm_ul."):
                legacy_imports.append((relative_path, module))

    assert legacy_imports == []
