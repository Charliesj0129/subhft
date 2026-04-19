"""Fubon family-resolver populator tests.

Mirrors ``test_shioaji_family_populator`` structure but sources symbols
from a YAML-like dict list rather than a Shioaji ``Contracts`` tree,
reflecting Fubon's contract surface.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from hft_platform.contracts.family_resolver import ContractFamilyResolver
from hft_platform.contracts.ref import ContractFamily, FamilyCode, Product
from hft_platform.feed_adapter.fubon.family_populator import (
    _extract_code,
    _get_symbols,
    populate_resolver_from_fubon,
)


class _FakeRuntime:
    def __init__(self, symbols: list[dict]) -> None:
        self.symbols = list(symbols)


class _FakeFacade:
    """Mimics FubonClientFacade that exposes a nested ``_contracts_runtime``."""

    def __init__(self, symbols: list[dict]) -> None:
        self._contracts_runtime = _FakeRuntime(symbols)


class TestSymbolExtraction:
    def test_none_source_yields_empty(self) -> None:
        assert _get_symbols(None) == []

    def test_list_source_returned_verbatim(self) -> None:
        src = [{"code": "X"}]
        assert _get_symbols(src) == src

    def test_runtime_symbols_attr(self) -> None:
        runtime = _FakeRuntime([{"code": "TMFE6"}])
        assert _get_symbols(runtime) == [{"code": "TMFE6"}]

    def test_facade_chain(self) -> None:
        facade = _FakeFacade([{"code": "TMFE6"}])
        assert _get_symbols(facade) == [{"code": "TMFE6"}]

    def test_code_from_dict(self) -> None:
        assert _extract_code({"code": "TMFE6"}) == "TMFE6"

    def test_code_from_symbol_key(self) -> None:
        assert _extract_code({"symbol": "TMFE6"}) == "TMFE6"

    def test_code_from_attr(self) -> None:
        obj = SimpleNamespace(code="TMFE6")
        assert _extract_code(obj) == "TMFE6"

    def test_blank_code_returns_none(self) -> None:
        assert _extract_code({"code": ""}) is None
        assert _extract_code({"code": "   "}) is None


class TestPopulator:
    def test_single_root_multiple_expiries(self) -> None:
        runtime = _FakeRuntime(
            [
                {"code": "TMFE6", "exchange": "TAIFEX"},
                {"code": "TMFF6", "exchange": "TAIFEX"},
                {"code": "TMFG6", "exchange": "TAIFEX"},
            ]
        )
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        assert count == 2
        r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        r2 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R2)
        assert resolver.resolve_family(r1).expiry.month == 5
        assert resolver.resolve_family(r2).expiry.month == 6

    def test_alias_codes_skipped(self) -> None:
        """TMFR1 parses to ContractFamily (not FutureRef) — silently dropped."""
        runtime = _FakeRuntime(
            [
                {"code": "TMFR1"},
                {"code": "TMFR2"},
                {"code": "TMFE6"},
            ]
        )
        resolver = ContractFamilyResolver()
        populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        ref = resolver.resolve_family(r1)
        assert ref is not None
        assert ref.display() == "TMFE6"

    def test_stock_codes_ignored(self) -> None:
        runtime = _FakeRuntime(
            [
                {"code": "2330", "exchange": "TSE"},
                {"code": "TMFE6", "exchange": "TAIFEX"},
            ]
        )
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        assert count == 1

    def test_option_codes_ignored(self) -> None:
        runtime = _FakeRuntime(
            [
                {"code": "TXO202605C23000"},
                {"code": "TXFE6"},
            ]
        )
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        assert count == 1

    def test_expired_contracts_dropped(self) -> None:
        # parse_display uses end-of-month as the expiry placeholder, so
        # TMFD6 (April) is still active on 2026-04-30 and drops on 2026-05-01.
        runtime = _FakeRuntime(
            [
                {"code": "TMFC6"},
                {"code": "TMFD6"},
                {"code": "TMFE6"},
            ]
        )
        resolver = ContractFamilyResolver()
        populate_resolver_from_fubon(resolver, runtime, today=date(2026, 5, 1))
        r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        ref = resolver.resolve_family(r1)
        assert ref is not None
        assert ref.display() == "TMFE6"

    def test_multiple_roots(self) -> None:
        runtime = _FakeRuntime(
            [
                {"code": "TMFE6"},
                {"code": "TXFE6"},
            ]
        )
        resolver = ContractFamilyResolver()
        populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        assert resolver.resolve_family(ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)) is not None
        assert resolver.resolve_family(ContractFamily(Product.FUTURE, "TXF", FamilyCode.R1)) is not None

    def test_none_source_is_noop(self) -> None:
        resolver = ContractFamilyResolver()
        assert populate_resolver_from_fubon(resolver, None, today=date(2026, 4, 19)) == 0

    def test_empty_symbols_is_noop(self) -> None:
        resolver = ContractFamilyResolver()
        runtime = _FakeRuntime([])
        assert populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19)) == 0

    def test_facade_symbols_chain(self) -> None:
        facade = _FakeFacade([{"code": "TMFE6"}])
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_fubon(resolver, facade, today=date(2026, 4, 19))
        assert count == 1

    def test_raw_list_source(self) -> None:
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_fubon(resolver, [{"code": "TMFE6"}], today=date(2026, 4, 19))
        assert count == 1

    def test_symbol_field_key_also_accepted(self) -> None:
        runtime = _FakeRuntime([{"symbol": "TMFE6"}])
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        assert count == 1

    def test_idempotent_repeat_call_produces_no_rebinds(self) -> None:
        runtime = _FakeRuntime([{"code": "TMFE6"}])
        resolver = ContractFamilyResolver()
        events: list = []
        resolver.add_hook(events.append)

        populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        first = len(events)
        populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        assert len(events) == first

    def test_rollover_refires_hooks(self) -> None:
        runtime_before = _FakeRuntime([{"code": "TMFE6"}, {"code": "TMFF6"}])
        runtime_after = _FakeRuntime([{"code": "TMFF6"}, {"code": "TMFG6"}])
        resolver = ContractFamilyResolver()
        events: list = []
        resolver.add_hook(events.append)

        populate_resolver_from_fubon(resolver, runtime_before, today=date(2026, 5, 20))
        first = len(events)
        populate_resolver_from_fubon(resolver, runtime_after, today=date(2026, 5, 22))
        assert len(events) > first
        r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        assert resolver.resolve_family(r1).display() == "TMFF6"

    def test_garbage_symbol_entries_do_not_crash(self) -> None:
        runtime = _FakeRuntime(
            [
                {"code": "not a valid code!!"},
                {"code": "TMFE6"},
                {},  # missing code
                {"code": None},
            ]
        )
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_fubon(resolver, runtime, today=date(2026, 4, 19))
        assert count == 1
