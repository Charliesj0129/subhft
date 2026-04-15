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
        sm.alias_to_actual = {"TXFC0": "TXFE6"}
        assert sm.resolve_symbol("TXFC0") == "TXFE6"
        assert sm.resolve_symbol("UNKNOWN") == "UNKNOWN"

    def test_resolve_symbols_set(self) -> None:
        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.alias_to_actual = {"TXFC0": "TXFE6", "TMFC0": "TMFE6"}
        result = sm.resolve_symbols({"TXFC0", "TMFC0", "2330"})
        assert result == {"TXFE6", "TMFE6", "2330"}

    def test_set_alias_map_merges(self) -> None:
        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.alias_to_actual = {"TXFC0": "TXFD6"}
        sm.set_alias_map({"TMFC0": "TMFE6"})
        assert sm.alias_to_actual == {"TXFC0": "TXFD6", "TMFC0": "TMFE6"}

    def test_set_alias_map_overwrites(self) -> None:
        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.alias_to_actual = {"TXFC0": "TXFD6"}
        sm.set_alias_map({"TXFC0": "TXFE6"})
        assert sm.alias_to_actual["TXFC0"] == "TXFE6"


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
        # Original config uses TMFC0
        alias_map = {"TMFC0": "TMFE6"}
        gov.resolve_symbol_aliases(alias_map)

        # Verify tracks were updated
        for track in gov._tracks.values():
            for sym in track.symbols:
                assert sym != "TMFC0", f"Alias TMFC0 should have been resolved to TMFE6 in track {track.name}"

    def test_resolve_registers_actual_in_gate(self) -> None:
        from hft_platform.ops.session_governor import SessionGovernor, SessionPhase

        gov = SessionGovernor()
        # Set a track phase first
        for track_name in gov._tracks:
            gov._track_gate.set_track_phase(track_name, SessionPhase.OPEN)

        alias_map = {"TMFC0": "TMFE6"}
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
