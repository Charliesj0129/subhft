"""Regression guard: contract-category enumeration across SDK generations.

Shioaji 1.3.3 exposes ``Contracts.Futures`` as a pydantic dict-like whose
``.keys()`` yields root groups; the 1.5.x Rust core drops the dict protocol
(``.keys()`` raises ``ContractCategory 'FUT' has no group 'keys'``) and
iterates flat, yielding contract objects directly. The 2026-07-17 old-host
deploy found three silently-degraded consumers of the legacy pattern
(contract_fetcher, contracts_runtime hourly refresh, family_populator), so
both shapes are pinned here.
"""

from __future__ import annotations

from typing import Any

import pytest

from hft_platform.feed_adapter.shioaji._compat import (
    contract_category_groups,
    iter_contract_category,
)


class _Contract:
    def __init__(self, code: str, category: str = "") -> None:
        self.code = code
        self.category = category


class _LegacyCategory:
    """1.3.3-style: dict protocol with root groups."""

    def __init__(self, groups: dict[str, list[_Contract]]) -> None:
        self._groups = groups

    def keys(self) -> list[str]:
        return list(self._groups.keys())

    def __getitem__(self, root: str) -> list[_Contract]:
        return self._groups[root]


class _RustCategory:
    """1.5.x-style: no dict protocol, flat iteration over contracts."""

    def __init__(self, contracts: list[_Contract]) -> None:
        self._contracts = contracts

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"ContractCategory 'FUT' has no group '{name}'")

    def __iter__(self) -> Any:
        return iter(self._contracts)


_TXF_H6 = _Contract("TXFH6", "TXF")
_TXF_I6 = _Contract("TXFI6", "TXF")
_MXF_H6 = _Contract("MXFH6", "MXF")


@pytest.mark.unit
def test_iter_contract_category_yields_leaf_contracts_on_legacy_dict_sdk() -> None:
    category = _LegacyCategory({"TXF": [_TXF_H6, _TXF_I6], "MXF": [_MXF_H6]})
    codes = [c.code for c in iter_contract_category(category)]
    assert codes == ["TXFH6", "TXFI6", "MXFH6"]


@pytest.mark.unit
def test_iter_contract_category_yields_leaf_contracts_on_flat_rust_sdk() -> None:
    category = _RustCategory([_TXF_H6, _TXF_I6, _MXF_H6])
    codes = [c.code for c in iter_contract_category(category)]
    assert codes == ["TXFH6", "TXFI6", "MXFH6"]


@pytest.mark.unit
def test_contract_category_groups_preserves_roots_on_legacy_dict_sdk() -> None:
    category = _LegacyCategory({"TXF": [_TXF_H6, _TXF_I6], "MXF": [_MXF_H6]})
    groups = contract_category_groups(category)
    assert {root: [c.code for c in cs] for root, cs in groups.items()} == {
        "TXF": ["TXFH6", "TXFI6"],
        "MXF": ["MXFH6"],
    }


@pytest.mark.unit
def test_contract_category_groups_regroups_by_category_on_flat_rust_sdk() -> None:
    category = _RustCategory([_TXF_H6, _MXF_H6, _TXF_I6])
    groups = contract_category_groups(category)
    assert {root: [c.code for c in cs] for root, cs in groups.items()} == {
        "TXF": ["TXFH6", "TXFI6"],
        "MXF": ["MXFH6"],
    }


@pytest.mark.unit
def test_contract_category_groups_buckets_missing_category_under_empty_root() -> None:
    orphan = _Contract("XXXH6", "")
    groups = contract_category_groups(_RustCategory([orphan]))
    assert groups == {"": [orphan]}
