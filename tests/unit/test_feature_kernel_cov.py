"""Coverage tests for feature/kernel.py — targeting 80%+ line coverage."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# _top_qty
# ---------------------------------------------------------------------------


def test_top_qty_none():
    from hft_platform.feature.kernel import _top_qty

    assert _top_qty(None) is None


def test_top_qty_empty_list():
    from hft_platform.feature.kernel import _top_qty

    assert _top_qty([]) == 0


def test_top_qty_list_with_entries():
    from hft_platform.feature.kernel import _top_qty

    side = [[100, 5], [99, 3]]
    assert _top_qty(side) == 5


def test_top_qty_list_single_entry():
    from hft_platform.feature.kernel import _top_qty

    side = [[100]]  # only price, no volume
    assert _top_qty(side) == 0


def test_top_qty_numpy_array():
    from hft_platform.feature.kernel import _top_qty

    side = np.array([[100.0, 5.0], [99.0, 3.0]])
    result = _top_qty(side)
    assert result == 5


def test_top_qty_numpy_empty():
    from hft_platform.feature.kernel import _top_qty

    side = np.empty((0, 2))
    result = _top_qty(side)
    assert result == 0


def test_top_qty_exception_handling():
    from hft_platform.feature.kernel import _top_qty

    class _Bad:
        def __len__(self):
            raise RuntimeError("broken")

    result = _top_qty(_Bad())
    assert result is None


# ---------------------------------------------------------------------------
# extract_l1_qty
# ---------------------------------------------------------------------------


def test_extract_l1_qty_none_event():
    from hft_platform.feature.kernel import extract_l1_qty

    bq, aq = extract_l1_qty(None, 10, 20)
    assert bq == 10
    assert aq == 20


def test_extract_l1_qty_from_event_arrays():
    from hft_platform.feature.kernel import extract_l1_qty

    event = SimpleNamespace(
        bids=[[100, 5], [99, 3]],
        asks=[[101, 4], [102, 2]],
    )
    bq, aq = extract_l1_qty(event, 0, 0)
    assert bq == 5
    assert aq == 4


def test_extract_l1_qty_null_bid_side():
    from hft_platform.feature.kernel import extract_l1_qty

    event = SimpleNamespace(bids=None, asks=[[101, 4]])
    bq, aq = extract_l1_qty(event, 7, 0)
    assert bq == 7  # fallback
    assert aq == 4


def test_extract_l1_qty_null_ask_side():
    from hft_platform.feature.kernel import extract_l1_qty

    event = SimpleNamespace(bids=[[100, 5]], asks=None)
    bq, aq = extract_l1_qty(event, 0, 11)
    assert bq == 5
    assert aq == 11  # fallback


def test_extract_l1_qty_both_null():
    from hft_platform.feature.kernel import extract_l1_qty

    event = SimpleNamespace(bids=None, asks=None)
    bq, aq = extract_l1_qty(event, 3, 4)
    assert bq == 3
    assert aq == 4


# ---------------------------------------------------------------------------
# compute_ofi_l1_raw
# ---------------------------------------------------------------------------


def test_ofi_l1_bid_increased():
    from hft_platform.feature.kernel import compute_ofi_l1_raw

    # best_bid > prev_best_bid → b_flow = bid_qty
    result = compute_ofi_l1_raw(101, 102, 10, 8, 100, 102, 5, 8)
    # b_flow = 10, best_ask == prev_best_ask → a_flow = 8-8=0
    assert result == 10


def test_ofi_l1_bid_same():
    from hft_platform.feature.kernel import compute_ofi_l1_raw

    # best_bid == prev_best_bid → b_flow = delta
    result = compute_ofi_l1_raw(100, 101, 12, 8, 100, 101, 10, 8)
    # b_flow = 12-10=2, a_flow = 8-8=0
    assert result == 2


def test_ofi_l1_bid_decreased():
    from hft_platform.feature.kernel import compute_ofi_l1_raw

    # best_bid < prev_best_bid → b_flow = -prev_bid_qty
    result = compute_ofi_l1_raw(99, 101, 10, 8, 100, 101, 5, 8)
    # b_flow = -5, a_flow = 8-8=0
    assert result == -5


def test_ofi_l1_ask_increased():
    from hft_platform.feature.kernel import compute_ofi_l1_raw

    # best_ask > prev_best_ask → a_flow = -prev_ask_qty
    # best_bid == prev_best_bid → b_flow = bid_qty - prev_bid_qty
    result = compute_ofi_l1_raw(100, 102, 5, 8, 100, 101, 5, 6)
    # b_flow = 5-5=0, a_flow = -prev_ask_qty = -6 → result = 0 - (-6) = 6
    assert result == 6


# ---------------------------------------------------------------------------
# compute_changed_mask
# ---------------------------------------------------------------------------


def test_compute_changed_mask_all_changed():
    from hft_platform.feature.kernel import compute_changed_mask

    prev = (1, 2, 3)
    new = (4, 5, 6)
    mask = compute_changed_mask(prev, new)
    assert mask == 0b111


def test_compute_changed_mask_none_changed():
    from hft_platform.feature.kernel import compute_changed_mask

    prev = (1, 2, 3)
    new = (1, 2, 3)
    assert compute_changed_mask(prev, new) == 0


def test_compute_changed_mask_prev_none():
    from hft_platform.feature.kernel import compute_changed_mask

    new = (1, 2, 3)
    mask = compute_changed_mask(None, new)
    assert mask == 0b111


def test_compute_changed_mask_different_lengths():
    from hft_platform.feature.kernel import compute_changed_mask

    prev = (1, 2)
    new = (1, 2, 3)
    mask = compute_changed_mask(prev, new)
    assert mask == 0b111


def test_compute_changed_mask_empty():
    from hft_platform.feature.kernel import compute_changed_mask

    assert compute_changed_mask(None, ()) == 0


# ---------------------------------------------------------------------------
# LobFeatureKernel.compute
# ---------------------------------------------------------------------------


@pytest.fixture()
def lob_kernel():
    from hft_platform.feature.kernel import LobFeatureKernel

    return LobFeatureKernel(ema_alpha=0.2, ofi_enabled=True)


@pytest.fixture()
def fresh_state():
    from hft_platform.feature.kernel import SymbolState

    return SymbolState()


def test_lob_kernel_first_tick(lob_kernel, fresh_state):
    """First tick initializes EMA state."""
    result = lob_kernel.compute(
        fresh_state, bb=100000, ba=100100, mid=100050, spread=100, bd=50, ad=30, l1bq=10, l1aq=8
    )
    assert len(result) == 16
    assert fresh_state.initialized is True


def test_lob_kernel_second_tick(lob_kernel, fresh_state):
    """Second tick uses EMA update."""
    lob_kernel.compute(fresh_state, 100000, 100100, 100050, 100, 50, 30, 10, 8)
    result = lob_kernel.compute(fresh_state, 100050, 100150, 100100, 100, 55, 35, 12, 9)
    assert len(result) == 16
    assert result[2] == 100100  # mid


def test_lob_kernel_ofi_disabled(fresh_state):
    from hft_platform.feature.kernel import LobFeatureKernel

    k = LobFeatureKernel(ema_alpha=0.2, ofi_enabled=False)
    k.compute(fresh_state, 100000, 100100, 100050, 100, 50, 30, 10, 8)
    result = k.compute(fresh_state, 100010, 100110, 100060, 100, 52, 32, 11, 9)
    # OFI should be 0 when disabled
    assert result[11] == 0  # ofi_l1_raw
    assert result[12] == 0  # ofi_l1_cum


def test_lob_kernel_zero_depth(lob_kernel, fresh_state):
    """Zero total depth produces zero imbalance."""
    result = lob_kernel.compute(fresh_state, 100000, 100100, 100050, 100, 0, 0, 0, 0)
    assert result[6] == 0  # imbalance_ppm


def test_lob_kernel_updates_prev_state(lob_kernel, fresh_state):
    lob_kernel.compute(fresh_state, 100000, 100100, 100050, 100, 50, 30, 10, 8)
    assert fresh_state.prev_best_bid == 100000
    assert fresh_state.prev_best_ask == 100100
    assert fresh_state.prev_l1_bid_qty == 10
    assert fresh_state.prev_l1_ask_qty == 8


# ---------------------------------------------------------------------------
# RustFeatureKernelAdapter
# ---------------------------------------------------------------------------


def test_rust_kernel_adapter_rust_unavailable():
    """When Rust is unavailable, compute returns None."""
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", None):
        adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
        result = adapter.compute("TSMC", 100000, 100100, 100050, 100, 50, 30, 10, 8)
        assert result is None


def test_rust_kernel_adapter_fused_unavailable():
    """When Rust pipeline unavailable, compute_fused returns None."""
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", None):
        adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
        result = adapter.compute_fused("TSMC", 100000, 100100, 100050, 100, 50, 30, 10, 8, 0)
        assert result is None


def test_rust_kernel_adapter_reset_symbol():
    """reset_symbol removes cached kernel and pipeline."""
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
    # Inject a mock kernel
    mock_kernel = MagicMock()
    mock_kernel.reset = MagicMock()
    adapter._kernels["TSMC"] = mock_kernel
    adapter.reset_symbol("TSMC")
    assert "TSMC" not in adapter._kernels


def test_rust_kernel_adapter_reset_symbol_missing():
    """reset_symbol on unknown symbol should not raise."""
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
    adapter.reset_symbol("UNKNOWN")  # Should not raise


def test_rust_kernel_adapter_reset_all():
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
    adapter._kernels["A"] = MagicMock()
    adapter._kernels["B"] = MagicMock()
    adapter._pipelines["A"] = MagicMock()
    adapter.reset_all()
    assert len(adapter._kernels) == 0
    assert len(adapter._pipelines) == 0


def test_rust_kernel_adapter_compute_with_rust(monkeypatch):
    """With mock Rust kernel, compute returns a tuple."""
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    mock_kernel_cls = MagicMock()
    mock_kernel_instance = MagicMock()
    mock_kernel_instance.update.return_value = tuple(range(16))
    mock_kernel_cls.return_value = mock_kernel_instance

    with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
        adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
        result = adapter.compute("TSMC", 100000, 100100, 100050, 100, 50, 30, 10, 8)
        assert isinstance(result, tuple)
        assert len(result) == 16


def test_rust_kernel_adapter_compute_fused_with_rust():
    """With mock Rust pipeline, compute_fused returns (values, changed_mask, warmup_mask)."""
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    mock_pipeline_cls = MagicMock()
    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.process.return_value = (list(range(16)), 0b1111, 0b11)
    mock_pipeline_cls.return_value = mock_pipeline_instance

    with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", mock_pipeline_cls):
        adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
        result = adapter.compute_fused("TSMC", 100000, 100100, 100050, 100, 50, 30, 10, 8, 3)
        assert result is not None
        values, changed_mask, warmup_mask = result
        assert isinstance(values, tuple)


def test_rust_kernel_adapter_compute_fused_exception():
    """compute_fused catches exceptions and returns None."""
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    mock_pipeline_cls = MagicMock()
    mock_pipeline_instance = MagicMock()
    mock_pipeline_instance.process.side_effect = RuntimeError("fail")
    mock_pipeline_cls.return_value = mock_pipeline_instance

    with patch("hft_platform.feature.kernel._RUST_FEATURE_PIPELINE_V1", mock_pipeline_cls):
        adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
        result = adapter.compute_fused("TSMC", 100000, 100100, 100050, 100, 50, 30, 10, 8, 3)
        assert result is None


def test_rust_kernel_adapter_reset_symbol_with_reset_method():
    """reset_symbol calls .reset() on cached kernel if available."""
    from hft_platform.feature.kernel import RustFeatureKernelAdapter

    adapter = RustFeatureKernelAdapter(ema_alpha=0.2, warmup_thresholds=[8, 8])
    mock_kernel = MagicMock()
    mock_pipeline = MagicMock()
    adapter._kernels["TSMC"] = mock_kernel
    adapter._pipelines["TSMC"] = mock_pipeline
    adapter.reset_symbol("TSMC")
    mock_kernel.reset.assert_called_once()
    mock_pipeline.reset.assert_called_once()


# ---------------------------------------------------------------------------
# rust_backend_available
# ---------------------------------------------------------------------------


def test_rust_backend_available_false():
    with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", None):
        from hft_platform.feature.kernel import rust_backend_available

        assert rust_backend_available() is False


def test_rust_backend_available_true():
    with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", MagicMock()):
        from hft_platform.feature.kernel import rust_backend_available

        assert rust_backend_available() is True


# ---------------------------------------------------------------------------
# SymbolState
# ---------------------------------------------------------------------------


def test_symbol_state_update_output():
    from hft_platform.feature.kernel import SymbolState

    s = SymbolState()
    s.update_output(1, 1000, 2000, (1, 2, 3), 5, 0)
    assert s.seq == 1
    assert s.source_ts_ns == 1000
    assert s.local_ts_ns == 2000
    assert s.values == (1, 2, 3)
    assert s.warm_count == 5
    assert s.quality_flags == 0
