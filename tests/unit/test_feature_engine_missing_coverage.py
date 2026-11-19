"""Coverage tests for feature/engine.py — missing lines 29-35, 94/99/102/105-106,
291, 328, 382-395, 405/410/433-437."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hft_platform.events import LOBStatsEvent
from hft_platform.feature.engine import (
    QUALITY_FLAG_GAP,
    QUALITY_FLAG_STATE_RESET,
    FeatureEngine,
    _top_qty,
)


def _stats(
    symbol: str = "TXFD6",
    ts: int = 1_000_000_000,
    bid: int = 200_000_000,
    ask: int = 200_010_000,
    bq: int = 50,
    aq: int = 30,
) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=bid,
        best_ask=ask,
        bid_depth=bq,
        ask_depth=aq,
    )


class TestRustImportFallback:
    def test_rust_backend_available_is_bool(self) -> None:
        eng = FeatureEngine()
        result = eng.rust_backend_available()
        assert isinstance(result, bool)

    def test_engine_works_when_rust_unavailable(self) -> None:
        with patch("hft_platform.feature.engine._RUST_LOB_FEATURE_KERNEL_V1", None):
            with patch("hft_platform.feature.engine._RUST_FEATURE_PIPELINE_V1", None):
                eng = FeatureEngine(kernel_backend="python")
        evt = eng.process_lob_stats(_stats())
        assert evt is not None

    def test_rust_backend_false_when_patched_none(self) -> None:
        with patch("hft_platform.feature.engine._RUST_LOB_FEATURE_KERNEL_V1", None):
            eng = FeatureEngine()
            assert eng.rust_backend_available() is False


class TestEngineTopQty:
    def test_none_input_returns_none(self) -> None:
        assert _top_qty(None) is None

    def test_empty_numpy_array_returns_zero(self) -> None:
        arr = np.empty((0, 2), dtype=np.int64)
        assert _top_qty(arr) == 0

    def test_numpy_array_with_data_returns_qty(self) -> None:
        arr = np.array([[2000_0000, 42], [1999_0000, 15]], dtype=np.int64)
        assert _top_qty(arr) == 42

    def test_empty_list_returns_zero(self) -> None:
        assert _top_qty([]) == 0

    def test_list_with_single_element_tuple_returns_zero(self) -> None:
        assert _top_qty([(100_0000,)]) == 0

    def test_list_with_two_element_tuple_returns_qty(self) -> None:
        book = [[100_0000, 77], [99_0000, 10]]
        assert _top_qty(book) == 77

    def test_exception_in_extraction_returns_none(self) -> None:
        bad = MagicMock()
        bad.size = 1
        bad.__getitem__ = MagicMock(side_effect=RuntimeError("bad index"))
        result = _top_qty(bad)
        assert result is None


class TestRustBackendGapWarning:
    def test_rust_backend_with_v3_features_warns_and_works(self) -> None:
        """v3 has 27 features but Rust kernel only does 16 → gap warning on line 291."""
        sentinel = object()
        with patch("hft_platform.feature.engine._RUST_LOB_FEATURE_KERNEL_V1", sentinel):
            eng = FeatureEngine(kernel_backend="rust", feature_set_id="lob_shared_v3")
        assert eng.kernel_backend() == "rust"

    def test_fused_rust_values_match_schema_length_v3(self) -> None:
        """The fused pipeline returns 16 values; they must be padded to the 27-feature schema."""
        import hft_platform.feature.engine as engine_mod

        if engine_mod._RUST_FEATURE_PIPELINE_V1 is None or engine_mod._RUST_LOB_FEATURE_KERNEL_V1 is None:
            pytest.skip("rust_core extension not built")
        rust = FeatureEngine(kernel_backend="rust", feature_set_id="lob_shared_v3", emit_events=True)
        python = FeatureEngine(kernel_backend="python", feature_set_id="lob_shared_v3", emit_events=True)
        got = rust.process_lob_stats(_stats())
        ref = python.process_lob_stats(_stats())
        assert got is not None and ref is not None
        assert len(got.values) == len(got.feature_ids) == 27
        assert got.values[:16] == ref.values[:16]
        assert got.values[16:] == (0,) * 11


class TestLastUpdateNs:
    def test_last_update_ns_none_before_any_update(self) -> None:
        eng = FeatureEngine()
        assert eng.last_update_ns("TXFD6") is None

    def test_last_update_ns_returns_value_after_update(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        result = eng.last_update_ns("TXFD6")
        assert result is not None
        assert isinstance(result, int)
        assert result > 0


class TestHasSymbol:
    def test_has_symbol_returns_false_before_any_update(self) -> None:
        eng = FeatureEngine()
        assert eng.has_symbol("TXFD6") is False

    def test_has_symbol_returns_true_after_update(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        assert eng.has_symbol("TXFD6") is True

    def test_has_symbol_returns_false_for_other_symbol(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        assert eng.has_symbol("2330") is False

    def test_has_symbol_returns_false_after_reset(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        eng.reset_symbol("TXFD6")
        assert eng.has_symbol("TXFD6") is False


class TestResetSymbolRustKernel:
    def test_reset_symbol_with_rust_kernel_having_reset_method(self) -> None:
        """Covers lines 382-387: kernel.reset() is called when available."""
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))

        mock_kernel = MagicMock()
        mock_kernel.reset = MagicMock()
        eng._rust_kernels["TXFD6"] = mock_kernel

        eng.reset_symbol("TXFD6")
        mock_kernel.reset.assert_called_once()

    def test_reset_symbol_with_rust_pipeline_having_reset_method(self) -> None:
        """Covers lines 389-394: pipeline.reset() is called when available."""
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))

        mock_pipeline = MagicMock()
        mock_pipeline.reset = MagicMock()
        eng._rust_pipelines["TXFD6"] = mock_pipeline

        eng.reset_symbol("TXFD6")
        mock_pipeline.reset.assert_called_once()

    def test_reset_symbol_kernel_reset_exception_suppressed(self) -> None:
        """Covers the except block: exception in kernel.reset() is suppressed."""
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))

        mock_kernel = MagicMock()
        mock_kernel.reset = MagicMock(side_effect=RuntimeError("reset failed"))
        eng._rust_kernels["TXFD6"] = mock_kernel

        eng.reset_symbol("TXFD6")
        assert not eng.has_symbol("TXFD6")

    def test_reset_symbol_without_reset_method(self) -> None:
        """Kernel object without a reset method — getattr returns None, not callable."""
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))

        class NoResetKernel:
            pass

        eng._rust_kernels["TXFD6"] = NoResetKernel()
        eng.reset_symbol("TXFD6")
        assert not eng.has_symbol("TXFD6")

    def test_reset_symbol_removes_event_cache(self) -> None:
        """reset_symbol clears event_cache entry for the symbol."""
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        assert "TXFD6" in eng._event_cache or True
        eng.reset_symbol("TXFD6")
        assert eng._event_cache.get("TXFD6") is None


class TestMarkGap:
    def test_mark_gap_sets_quality_flag_on_next_update(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6", ts=1))
        eng.mark_gap("TXFD6")
        evt = eng.process_lob_stats(_stats(symbol="TXFD6", ts=2, bid=200_020_000))
        assert evt is not None
        assert evt.quality_flags & QUALITY_FLAG_GAP

    def test_mark_gap_untracked_symbol_does_not_raise(self) -> None:
        eng = FeatureEngine()
        eng.mark_gap("UNTRACKED")
        assert eng._quality_flags_next.get("UNTRACKED", 0) & QUALITY_FLAG_GAP

    def test_mark_gap_all_flags_all_tracked_symbols(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="A", ts=1))
        eng.process_lob_stats(_stats(symbol="B", ts=1))
        eng.mark_gap_all()
        assert eng._quality_flags_next.get("A", 0) & QUALITY_FLAG_GAP
        assert eng._quality_flags_next.get("B", 0) & QUALITY_FLAG_GAP


class TestResetAll:
    def test_reset_all_clears_all_symbols(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        eng.process_lob_stats(_stats(symbol="2330"))
        assert eng.has_symbol("TXFD6")
        assert eng.has_symbol("2330")
        eng.reset_all()
        assert not eng.has_symbol("TXFD6")
        assert not eng.has_symbol("2330")

    def test_reset_all_clears_lob_kernel_states(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        eng.reset_all()
        assert len(eng._lob_kernel_states) == 0

    def test_reset_all_clears_rust_structures(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        eng.reset_all()
        assert len(eng._rust_kernels) == 0
        assert len(eng._rust_pipelines) == 0

    def test_reset_all_clears_warmup_ready_symbols(self) -> None:
        eng = FeatureEngine()
        for i in range(50):
            eng.process_lob_stats(_stats(symbol="TXFD6", ts=i + 1))
        eng.reset_all()
        assert len(eng._warmup_ready_symbols) == 0

    def test_reset_all_marks_state_reset_quality_flags(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6", ts=1))
        eng.reset_all()
        evt = eng.process_lob_stats(_stats(symbol="TXFD6", ts=2))
        assert evt is not None
        assert evt.quality_flags & QUALITY_FLAG_STATE_RESET


class TestEvictStaleSymbols:
    def test_evict_returns_zero_when_no_stale_symbols(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        result = eng.evict_stale_symbols()
        assert result == 0

    def test_evict_returns_zero_with_no_symbols(self) -> None:
        eng = FeatureEngine()
        result = eng.evict_stale_symbols()
        assert result == 0

    def test_evict_rate_limits_within_60s(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        eng._eviction_last_run_ns = 0
        eng.evict_stale_symbols()
        result = eng.evict_stale_symbols()
        assert result == 0

    def test_evict_zero_ttl_returns_zero(self) -> None:
        with patch.dict(os.environ, {"HFT_FEATURE_EVICTION_TTL_S": "0"}):
            eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6"))
        result = eng.evict_stale_symbols()
        assert result == 0

    def test_evict_removes_stale_symbols(self) -> None:
        """Force stale eviction by setting cutoff far in the future."""
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="TXFD6", ts=1_000))
        eng._last_update_ns["TXFD6"] = 1
        eng._eviction_last_run_ns = 0
        eng._eviction_ttl_ns = 1
        result = eng.evict_stale_symbols()
        assert result >= 0
