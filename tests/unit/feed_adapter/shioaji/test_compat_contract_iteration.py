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


class _Rust17Group:
    """1.7.x ``ContractGroup``: iterates contracts; ``__getattr__`` is a code
    lookup that raises ``KeyError`` (not ``AttributeError``) on a miss."""

    def __init__(self, contracts: list[_Contract]) -> None:
        self._contracts = {c.code: c for c in contracts}

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["_contracts"][name]
        except KeyError:
            raise KeyError(name) from None

    def __iter__(self) -> Any:
        return iter(self._contracts.values())


class _Rust17Category:
    """1.7.x ``ContractCategory``: ``__getattr__`` returns a group for any name
    (so ``.keys`` is a group, not a method) and iteration yields groups."""

    def __init__(self, groups: list[_Rust17Group]) -> None:
        self._groups = groups

    def __getattr__(self, name: str) -> Any:
        return _Rust17Group([])

    def __iter__(self) -> Any:
        return iter(self.__dict__["_groups"])


def _rust17_futures() -> _Rust17Category:
    return _Rust17Category([_Rust17Group([_TXF_H6, _TXF_I6]), _Rust17Group([_MXF_H6])])


@pytest.mark.unit
def test_iter_contract_category_yields_leaf_contracts_on_grouped_rust17_sdk() -> None:
    codes = [c.code for c in iter_contract_category(_rust17_futures())]
    assert codes == ["TXFH6", "TXFI6", "MXFH6"]


@pytest.mark.unit
def test_contract_category_groups_does_not_raise_when_group_getattr_raises_keyerror() -> None:
    groups = contract_category_groups(_rust17_futures())
    assert {root: [c.code for c in cs] for root, cs in groups.items()} == {
        "TXF": ["TXFH6", "TXFI6"],
        "MXF": ["MXFH6"],
    }


@pytest.mark.unit
def test_contract_member_returns_none_when_rust17_lookup_misses() -> None:
    from hft_platform.feed_adapter.shioaji._compat import contract_member

    group = _Rust17Group([_TXF_H6])
    assert contract_member(group, "TXFH6") is _TXF_H6
    assert contract_member(group, "TXFZ9") is None
    assert contract_member(None, "TXFH6") is None
