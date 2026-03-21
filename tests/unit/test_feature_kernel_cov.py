"""Additional coverage tests for feature/kernel.py.

Targets the branches not covered by test_feature_kernel.py:
- RustFeatureKernelAdapter: compute (rust path, TypeError fallback), compute_fused,
  reset_symbol with existing kernel/pipeline, reset_all, fallback_warned path
- LobFeatureKernel: second-tick OFI path (ofi_enabled=True, initialized=True),
  EMA updates on subsequent ticks
- _top_qty: side with 'size' attribute (numpy ndarray), single-element row
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hft_platform.feature.kernel import (
    LobFeatureKernel,
    RustFeatureKernelAdapter,
    SymbolState,
    _top_qty,
    extract_l1_qty,
)

# ---------------------------------------------------------------------------
# _top_qty — numpy ndarray path (has 'size' attribute)
# ---------------------------------------------------------------------------


class TestTopQtyNumpyPath:
    def test_numpy_zero_size(self) -> None:
        """ndarray with size=0 should return 0."""
        arr = np.empty((0, 2), dtype=np.int64)
        assert arr.size == 0
        assert _top_qty(arr) == 0

    def test_numpy_nonzero_size(self) -> None:
        """ndarray with size>0 returns int(side[0][1])."""
        arr = np.array([[1000000, 42], [990000, 15]], dtype=np.int64)
        assert _top_qty(arr) == 42

    def test_numpy_raises_fallback_returns_none(self) -> None:
        """If accessing arr[0][1] raises, _top_qty returns None."""
        bad = MagicMock()
        bad.size = 1  # triggers the size branch
        bad.__getitem__ = MagicMock(side_effect=RuntimeError("boom"))
        result = _top_qty(bad)
        assert result is None


# ---------------------------------------------------------------------------
# extract_l1_qty — bq/aq None fallback branches
# ---------------------------------------------------------------------------


class TestExtractL1QtyNullPaths:
    class _BrokenSide:
        """Side object where _top_qty will return None."""

        def __len__(self) -> int:
            raise RuntimeError("broken")

    def test_both_sides_broken_uses_fallbacks(self) -> None:
        """When both bids and asks return None from _top_qty, fallbacks are used."""

        class _FakeEvent:
            bids = None
            asks = None

        bq, aq = extract_l1_qty(_FakeEvent(), bid_depth_fallback=8, ask_depth_fallback=4)
        assert bq == 8
        assert aq == 4

    def test_negative_extracted_qty_clamped_to_zero(self) -> None:
        """If _top_qty returns a negative (won't happen in practice but guard exists), clamped."""
        bq, aq = extract_l1_qty(None, bid_depth_fallback=-3, ask_depth_fallback=0)
        assert bq == 0
        assert aq == 0


# ---------------------------------------------------------------------------
# LobFeatureKernel — second-tick path (initialized=True, ofi_enabled=True)
# ---------------------------------------------------------------------------


class TestLobFeatureKernelSecondTick:
    """Tests that cover the `else` branch of `if not state.initialized`."""

    @pytest.fixture()
    def kernel_state(self) -> tuple[LobFeatureKernel, SymbolState]:
        kernel = LobFeatureKernel(ema_alpha=0.25, ofi_enabled=True)
        state = SymbolState()
        # Initialise
        kernel.compute(state, bb=100_0000, ba=101_0000, mid=100_5000, spread=1_0000, bd=200, ad=100, l1bq=50, l1aq=30)
        return kernel, state

    def test_second_tick_ofi_raw_nonzero_bid_up(self, kernel_state: tuple[LobFeatureKernel, SymbolState]) -> None:
        kernel, state = kernel_state
        vals = kernel.compute(
            state, bb=101_0000, ba=102_0000, mid=101_5000, spread=1_0000, bd=200, ad=100, l1bq=60, l1aq=30
        )
        # bid went up: b_flow = bid_qty = 60; ask went up: a_flow = -prev_ask_qty = -30; OFI = 60 - (-30) = 90
        assert vals[11] == 90  # ofi_l1_raw
        assert vals[12] == 90  # ofi_l1_cum
        assert vals[13] != 0  # ofi_l1_ema8 != 0 after nonzero ofi

    def test_second_tick_ofi_cumulates(self, kernel_state: tuple[LobFeatureKernel, SymbolState]) -> None:
        kernel, state = kernel_state
        # Tick 2
        kernel.compute(state, bb=101_0000, ba=102_0000, mid=101_5000, spread=1_0000, bd=200, ad=100, l1bq=60, l1aq=30)
        # Tick 3 — same bid/ask, same qty → b_flow=0, a_flow=0, OFI=0
        vals = kernel.compute(
            state, bb=101_0000, ba=102_0000, mid=101_5000, spread=1_0000, bd=200, ad=100, l1bq=60, l1aq=30
        )
        # cum stays at 90 (no new ofi added), ema decays
        assert vals[12] == 90  # ofi_l1_cum unchanged
        assert vals[11] == 0  # ofi_l1_raw zero

    def test_spread_ema_updates_on_second_tick(self, kernel_state: tuple[LobFeatureKernel, SymbolState]) -> None:
        kernel, state = kernel_state
        # Feed larger spread
        vals = kernel.compute(
            state, bb=100_0000, ba=102_0000, mid=101_0000, spread=2_0000, bd=200, ad=100, l1bq=50, l1aq=30
        )
        spread_ema = vals[14]
        # Expected: (1-0.25)*10000 + 0.25*20000 = 7500 + 5000 = 12500
        assert spread_ema == 12_500

    def test_depth_imbalance_ema_updates(self, kernel_state: tuple[LobFeatureKernel, SymbolState]) -> None:
        kernel, state = kernel_state
        # Equal l1 qty → l1_imbalance = 0
        vals = kernel.compute(
            state, bb=100_0000, ba=101_0000, mid=100_5000, spread=1_0000, bd=100, ad=100, l1bq=50, l1aq=50
        )
        # Previous first tick had l1bq=50, l1aq=30 → imbalance_ppm = (50-30)/(80)*1e6 = 250000
        # New: l1bq=50, l1aq=50 → 0 ppm
        # EMA: (1-0.25)*250000 + 0.25*0 = 187500
        depth_imb_ema = vals[15]
        assert depth_imb_ema == 187_500

    def test_ofi_disabled_second_tick_zeros(self) -> None:
        kernel = LobFeatureKernel(ema_alpha=0.25, ofi_enabled=False)
        state = SymbolState()
        kernel.compute(state, 100, 101, 100, 1, 50, 50, 10, 10)
        vals = kernel.compute(state, 101, 102, 101, 1, 50, 50, 12, 8)
        assert vals[11] == 0  # ofi_l1_raw zero
        assert vals[12] == 0  # ofi_l1_cum zero
        assert vals[13] == 0  # ofi_l1_ema8 zero

    def test_prev_state_updated_after_second_tick(self, kernel_state: tuple[LobFeatureKernel, SymbolState]) -> None:
        kernel, state = kernel_state
        kernel.compute(state, bb=105_0000, ba=106_0000, mid=105_5000, spread=1_0000, bd=300, ad=150, l1bq=70, l1aq=40)
        assert state.prev_best_bid == 105_0000
        assert state.prev_best_ask == 106_0000
        assert state.prev_l1_bid_qty == 70
        assert state.prev_l1_ask_qty == 40


# ---------------------------------------------------------------------------
# RustFeatureKernelAdapter — mocked Rust paths
# ---------------------------------------------------------------------------


class TestRustFeatureKernelAdapterMockedRust:
    """Test RustFeatureKernelAdapter with a mock Rust kernel to cover Rust paths."""

    def _make_adapter(self) -> RustFeatureKernelAdapter:
        return RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[2, 5])

    def test_compute_returns_none_when_rust_unavailable(self) -> None:
        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", None):
            result = adapter.compute("SYM", 100, 101, 100, 1, 50, 50, 10, 10)
        assert result is None

    def test_compute_creates_kernel_on_first_call(self) -> None:
        mock_kernel_cls = MagicMock()
        mock_kernel_instance = MagicMock()
        mock_kernel_instance.update.return_value = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)
        mock_kernel_cls.return_value = mock_kernel_instance

        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
            result = adapter.compute("SYM", 100, 101, 100, 1, 50, 50, 10, 10)

        assert result is not None
        assert isinstance(result, tuple)
        assert result[0] == 1
        mock_kernel_cls.assert_called_once_with(ema_alpha=0.2)
        assert "SYM" in adapter._kernels

    def test_compute_reuses_existing_kernel(self) -> None:
        mock_kernel_cls = MagicMock()
        mock_kernel_instance = MagicMock()
        mock_kernel_instance.update.return_value = tuple(range(16))
        mock_kernel_cls.return_value = mock_kernel_instance

        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
            adapter.compute("SYM", 100, 101, 100, 1, 50, 50, 10, 10)
            adapter.compute("SYM", 102, 103, 102, 1, 50, 50, 11, 9)

        # Kernel class instantiated only once
        assert mock_kernel_cls.call_count == 1
        assert mock_kernel_instance.update.call_count == 2

    def test_compute_handles_typeerror_on_ema_alpha_kwarg(self) -> None:
        """If LobFeatureKernelV1(ema_alpha=...) raises TypeError, falls back to no-arg init."""
        mock_kernel_cls = MagicMock()
        mock_kernel_instance = MagicMock()
        mock_kernel_instance.update.return_value = tuple(range(16))
        # First call with kwargs raises TypeError; second call (no args) succeeds
        mock_kernel_cls.side_effect = [TypeError("no kwarg"), None]
        mock_kernel_cls.return_value = mock_kernel_instance

        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
            # Replace side_effect with raising only on kwarg call
            calls: list[Any] = []

            def _factory(**kwargs: Any) -> MagicMock:
                if kwargs:
                    raise TypeError("no kwarg")
                return mock_kernel_instance

            mock_kernel_cls.side_effect = None
            mock_kernel_cls.return_value = None

            def _side_effect(*args: Any, **kwargs: Any) -> MagicMock:
                calls.append((args, kwargs))
                if kwargs:
                    raise TypeError("kwarg not supported")
                return mock_kernel_instance

            mock_kernel_cls.side_effect = _side_effect
            result = adapter.compute("SYM2", 100, 101, 100, 1, 50, 50, 10, 10)

        assert result is not None

    def test_compute_output_coerced_to_tuple(self) -> None:
        """If kernel.update returns a list, it must be coerced to tuple."""
        mock_kernel_cls = MagicMock()
        mock_kernel_instance = MagicMock()
        mock_kernel_instance.update.return_value = list(range(16))  # list, not tuple
        mock_kernel_cls.return_value = mock_kernel_instance

        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
            result = adapter.compute("SYM", 100, 101, 100, 1, 50, 50, 10, 10)

        assert isinstance(result, tuple)

    def test_compute_fused_returns_none_when_pipeline_unavailable(self) -> None:
        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", None):
            result = adapter.compute_fused("SYM", 100, 101, 100, 1, 50, 50, 10, 10, warm_count=1)
        assert result is None

    def test_compute_fused_creates_pipeline_and_returns_result(self) -> None:
        mock_pipeline_cls = MagicMock()
        mock_pipeline_instance = MagicMock()
        values_list = list(range(16))
        mock_pipeline_instance.process.return_value = (values_list, 0b1111, 0b0011)
        mock_pipeline_cls.return_value = mock_pipeline_instance

        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", mock_pipeline_cls):
            result = adapter.compute_fused("SYM", 100, 101, 100, 1, 50, 50, 10, 10, warm_count=2)

        assert result is not None
        values, changed_mask, warmup_mask = result
        assert isinstance(values, tuple)
        assert len(values) == 16
        assert changed_mask == 0b1111
        assert warmup_mask == 0b0011

    def test_compute_fused_reuses_existing_pipeline(self) -> None:
        mock_pipeline_cls = MagicMock()
        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.process.return_value = (list(range(16)), 0, 0)
        mock_pipeline_cls.return_value = mock_pipeline_instance

        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", mock_pipeline_cls):
            adapter.compute_fused("SYM", 100, 101, 100, 1, 50, 50, 10, 10, warm_count=1)
            adapter.compute_fused("SYM", 102, 103, 102, 1, 50, 50, 10, 10, warm_count=2)

        assert mock_pipeline_cls.call_count == 1
        assert mock_pipeline_instance.process.call_count == 2

    def test_compute_fused_returns_none_on_exception_and_logs_warning(self) -> None:
        """compute_fused catches exceptions, logs warning once per symbol, returns None."""
        mock_pipeline_cls = MagicMock()
        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.process.side_effect = RuntimeError("crash")
        mock_pipeline_cls.return_value = mock_pipeline_instance

        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", mock_pipeline_cls):
            result1 = adapter.compute_fused("FAIL_SYM", 100, 101, 100, 1, 50, 50, 10, 10, warm_count=1)
            result2 = adapter.compute_fused("FAIL_SYM", 100, 101, 100, 1, 50, 50, 10, 10, warm_count=2)

        assert result1 is None
        assert result2 is None
        # Warning logged only once (first failure)
        assert "FAIL_SYM" in adapter._fallback_warned

    def test_compute_fused_output_coerced_to_tuple(self) -> None:
        """If pipeline.process returns list for values, coerced to tuple."""
        mock_pipeline_cls = MagicMock()
        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.process.return_value = ([1, 2, 3], 7, 3)
        mock_pipeline_cls.return_value = mock_pipeline_instance

        adapter = self._make_adapter()
        with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", mock_pipeline_cls):
            result = adapter.compute_fused("SYM", 100, 101, 100, 1, 50, 50, 10, 10, warm_count=1)

        assert result is not None
        assert isinstance(result[0], tuple)


# ---------------------------------------------------------------------------
# RustFeatureKernelAdapter — reset_symbol / reset_all with existing entries
# ---------------------------------------------------------------------------


class TestRustFeatureKernelAdapterReset:
    def test_reset_symbol_removes_kernel_entry(self) -> None:
        mock_kernel_cls = MagicMock()
        mock_kernel_instance = MagicMock()
        mock_kernel_instance.update.return_value = tuple(range(16))
        mock_kernel_cls.return_value = mock_kernel_instance

        adapter = RustFeatureKernelAdapter(ema_alpha=0.1, warmup_thresholds=[2])
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
            adapter.compute("SYM_X", 100, 101, 100, 1, 50, 50, 10, 10)

        assert "SYM_X" in adapter._kernels
        adapter.reset_symbol("SYM_X")
        assert "SYM_X" not in adapter._kernels

    def test_reset_symbol_calls_kernel_reset_if_available(self) -> None:
        mock_kernel_cls = MagicMock()
        mock_kernel_instance = MagicMock()
        mock_kernel_instance.update.return_value = tuple(range(16))
        mock_kernel_cls.return_value = mock_kernel_instance

        adapter = RustFeatureKernelAdapter(ema_alpha=0.1, warmup_thresholds=[2])
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
            adapter.compute("SYM_Y", 100, 101, 100, 1, 50, 50, 10, 10)
        # Manually inject a kernel with a reset method
        mock_reset = MagicMock()
        mock_with_reset = MagicMock()
        mock_with_reset.reset = mock_reset
        adapter._kernels["SYM_Y"] = mock_with_reset
        adapter.reset_symbol("SYM_Y")
        mock_reset.assert_called_once()

    def test_reset_symbol_handles_reset_exception_gracefully(self) -> None:
        """If kernel.reset() raises, it's swallowed."""
        adapter = RustFeatureKernelAdapter(ema_alpha=0.1, warmup_thresholds=[2])
        mock_kernel = MagicMock()
        mock_kernel.reset.side_effect = RuntimeError("reset failed")
        adapter._kernels["ERR_SYM"] = mock_kernel
        # Should not raise
        adapter.reset_symbol("ERR_SYM")
        assert "ERR_SYM" not in adapter._kernels

    def test_reset_symbol_no_op_for_unknown_symbol(self) -> None:
        adapter = RustFeatureKernelAdapter(ema_alpha=0.1, warmup_thresholds=[2])
        adapter.reset_symbol("NONEXISTENT")  # Should not raise

    def test_reset_all_clears_all_kernels_and_pipelines(self) -> None:
        mock_kernel_cls = MagicMock()
        mock_kernel_instance = MagicMock()
        mock_kernel_instance.update.return_value = tuple(range(16))
        mock_kernel_cls.return_value = mock_kernel_instance

        adapter = RustFeatureKernelAdapter(ema_alpha=0.1, warmup_thresholds=[2])
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
            adapter.compute("A", 100, 101, 100, 1, 50, 50, 10, 10)
            adapter.compute("B", 100, 101, 100, 1, 50, 50, 10, 10)

        assert len(adapter._kernels) == 2
        adapter.reset_all()
        assert len(adapter._kernels) == 0
        assert len(adapter._pipelines) == 0

    def test_reset_symbol_removes_pipeline_entry(self) -> None:
        mock_pipeline_cls = MagicMock()
        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.process.return_value = (list(range(16)), 0, 0)
        mock_pipeline_cls.return_value = mock_pipeline_instance

        adapter = RustFeatureKernelAdapter(ema_alpha=0.1, warmup_thresholds=[2])
        with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", mock_pipeline_cls):
            adapter.compute_fused("PIPE_SYM", 100, 101, 100, 1, 50, 50, 10, 10, warm_count=1)

        assert "PIPE_SYM" in adapter._pipelines
        adapter.reset_symbol("PIPE_SYM")
        assert "PIPE_SYM" not in adapter._pipelines

    def test_reset_symbol_calls_pipeline_reset_if_available(self) -> None:
        adapter = RustFeatureKernelAdapter(ema_alpha=0.1, warmup_thresholds=[2])
        mock_pipeline = MagicMock()
        mock_reset = MagicMock()
        mock_pipeline.reset = mock_reset
        adapter._pipelines["SYM_P"] = mock_pipeline
        adapter.reset_symbol("SYM_P")
        mock_reset.assert_called_once()


# ---------------------------------------------------------------------------
# SymbolState — additional field checks
# ---------------------------------------------------------------------------


class TestSymbolStateFields:
    def test_all_rolling_fields_default_zero(self) -> None:
        s = SymbolState()
        assert s.prev_best_bid == 0
        assert s.prev_best_ask == 0
        assert s.prev_l1_bid_qty == 0
        assert s.prev_l1_ask_qty == 0
        assert s.ofi_l1_cum == 0
        assert s.ofi_l1_ema8 == 0.0
        assert s.spread_ema8 == 0.0
        assert s.imbalance_ema8_ppm == 0.0
        assert s.initialized is False

    def test_initialized_set_after_first_compute(self) -> None:
        kernel = LobFeatureKernel(ema_alpha=0.2, ofi_enabled=True)
        state = SymbolState()
        assert state.initialized is False
        kernel.compute(state, 100, 101, 100, 1, 50, 50, 10, 10)
        assert state.initialized is True
