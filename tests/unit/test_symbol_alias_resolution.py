"""Tests for C0/C1/R1/R2 symbol alias resolution.

Verifies that:
- SymbolMetadata resolves aliases to actual month codes
- Fee calculator uses root-prefix fallback
- SessionGovernor re-registers symbols on alias resolution
- StrategyRunner re-resolves strategy symbols after alias map update
"""

from __future__ import annotations

import pytest

from hft_platform.feed_adapter.normalizer import SymbolMetadata


class TestSymbolMetadataAliasResolution:
    """SymbolMetadata.resolve_symbol / resolve_symbols."""

    def test_resolve_symbol_with_no_alias_returns_input(self) -> None:
        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.alias_to_actual = {}
        assert sm.resolve_symbol("TXFD6") == "TXFD6"

    def test_resolve_symbol_with_alias_returns_actual(self) -> None:
        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.alias_to_actual = {"TXFR1": "TXFE6"}
        assert sm.resolve_symbol("TXFR1") == "TXFE6"
        assert sm.resolve_symbol("UNKNOWN") == "UNKNOWN"

    def test_resolve_symbols_set(self) -> None:
        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.alias_to_actual = {"TXFR1": "TXFE6", "TMFR1": "TMFE6"}
        result = sm.resolve_symbols({"TXFR1", "TMFR1", "2330"})
        assert result == {"TXFE6", "TMFE6", "2330"}

    def test_set_alias_map_merges(self) -> None:
        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.alias_to_actual = {"TXFR1": "TXFD6"}
        sm.meta = {}
        sm.set_alias_map({"TMFR1": "TMFE6"})
        assert sm.alias_to_actual == {"TXFR1": "TXFD6", "TMFR1": "TMFE6"}

    def test_set_alias_map_overwrites(self) -> None:
        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.alias_to_actual = {"TXFR1": "TXFD6"}
        sm.meta = {}
        sm.set_alias_map({"TXFR1": "TXFE6"})
        assert sm.alias_to_actual["TXFR1"] == "TXFE6"


class TestDeriveCallbackCode:
    """derive_callback_code: R1/R2/C0/C1 → actual month code from delivery_month."""

    def test_r1_with_delivery_month(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="TMFR1", delivery_month="2026/05")
        assert derive_callback_code(contract, "TMFR1") == "TMFE6"

    def test_r1_delivery_month_yyyymm(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="TXFR1", delivery_month="202605")
        assert derive_callback_code(contract, "TXFR1") == "TXFE6"

    def test_c0_with_delivery_month(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="TMFC0", delivery_month="2026/05")
        assert derive_callback_code(contract, "TMFC0") == "TMFE6"

    def test_r2_far_month(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="TXFR2", delivery_month="2026/06")
        assert derive_callback_code(contract, "TXFR2") == "TXFF6"

    def test_delivery_date_fallback(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="TMFR1", delivery_date="2026/05/20")
        assert derive_callback_code(contract, "TMFR1") == "TMFE6"

    def test_regular_code_passthrough(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="TMFE6")
        assert derive_callback_code(contract, "TMFE6") == "TMFE6"

    def test_no_delivery_info_falls_back_to_contract_code(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="TMFR1")
        assert derive_callback_code(contract, "TMFR1") == "TMFR1"

    def test_january_month_letter(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="TXFR1", delivery_month="2027/01")
        assert derive_callback_code(contract, "TXFR1") == "TXFA7"

    def test_december_month_letter(self) -> None:
        from types import SimpleNamespace

        from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

        contract = SimpleNamespace(code="MXFR1", delivery_month="2026/12")
        assert derive_callback_code(contract, "MXFR1") == "MXFL6"


class TestFeeCalculatorPrefixFallback:
    """Fee calculator root-prefix matching for unknown month codes."""

    def test_exact_root_match(self) -> None:
        from hft_platform.tca.fee_calculator import FeeCalculator

        calc = FeeCalculator.from_yaml("config/base/fees/futures.yaml")
        # TMF is in symbol_map → XMT
        result = calc._resolve_product("TMF")
        assert result == "XMT"

    def test_prefix_fallback_for_new_month_code(self) -> None:
        from hft_platform.tca.fee_calculator import FeeCalculator

        calc = FeeCalculator.from_yaml("config/base/fees/futures.yaml")
        # TMFE6 is NOT in symbol_map, but TMF is → XMT
        result = calc._resolve_product("TMFE6")
        assert result == "XMT"

    def test_prefix_fallback_txf(self) -> None:
        from hft_platform.tca.fee_calculator import FeeCalculator

        calc = FeeCalculator.from_yaml("config/base/fees/futures.yaml")
        result = calc._resolve_product("TXFG7")
        assert result == "TX"

    def test_unknown_symbol_returns_none(self) -> None:
        from hft_platform.tca.fee_calculator import FeeCalculator

        calc = FeeCalculator.from_yaml("config/base/fees/futures.yaml")
        result = calc._resolve_product("ZZZZZ9")
        assert result is None

    def test_stock_symbol_not_prefix_matched(self) -> None:
        from hft_platform.tca.fee_calculator import FeeCalculator

        calc = FeeCalculator.from_yaml("config/base/fees/futures.yaml")
        # Short symbols (< 5 chars) don't trigger prefix fallback
        result = calc._resolve_product("2330")
        assert result is None


class TestSessionGovernorAliasResolution:
    """SessionGovernor.resolve_symbol_aliases."""

    def test_resolve_updates_track_symbols(self) -> None:
        from hft_platform.ops.session_governor import SessionGovernor

        gov = SessionGovernor()
        # Original config uses TMFR1
        alias_map = {"TMFR1": "TMFE6"}
        gov.resolve_symbol_aliases(alias_map)

        # Verify tracks were updated
        for track in gov._tracks.values():
            for sym in track.symbols:
                assert sym != "TMFR1", f"Alias TMFR1 should have been resolved to TMFE6 in track {track.name}"

    def test_resolve_registers_actual_in_gate(self) -> None:
        from hft_platform.ops.session_governor import SessionGovernor, SessionPhase

        gov = SessionGovernor()
        # Set a track phase first
        for track_name in gov._tracks:
            gov._track_gate.set_track_phase(track_name, SessionPhase.OPEN)

        alias_map = {"TMFR1": "TMFE6"}
        gov.resolve_symbol_aliases(alias_map)

        # The actual code should now be registered in the gate
        phase = gov._track_gate.get_phase("TMFE6")
        assert phase == SessionPhase.OPEN

    def test_empty_alias_map_is_noop(self) -> None:
        from hft_platform.ops.session_governor import SessionGovernor

        gov = SessionGovernor()
        original_symbols = {
            name: list(track.symbols) for name, track in gov._tracks.items()
        }
        gov.resolve_symbol_aliases({})
        for name, track in gov._tracks.items():
            assert track.symbols == original_symbols[name]


class TestReportCollectorPrefixThreshold:
    """reports/collector.py root-prefix threshold lookup."""

    def test_root_match(self) -> None:
        from hft_platform.reports.collector import _get_large_trade_threshold

        assert _get_large_trade_threshold("TMF") == 30
        assert _get_large_trade_threshold("TXF") == 10

    def test_prefix_fallback(self) -> None:
        from hft_platform.reports.collector import _get_large_trade_threshold

        assert _get_large_trade_threshold("TMFE6") == 30
        assert _get_large_trade_threshold("TXFG7") == 10
        assert _get_large_trade_threshold("MXFH8") == 30

    def test_unknown_returns_default(self) -> None:
        from hft_platform.reports.collector import _get_large_trade_threshold

        assert _get_large_trade_threshold("ZZZZZ9") == 10

    def test_stock_exact_match(self) -> None:
        from hft_platform.reports.collector import _get_large_trade_threshold

        assert _get_large_trade_threshold("2330") == 100
