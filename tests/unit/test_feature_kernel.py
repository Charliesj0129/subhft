"""Unit tests for feature/kernel.py — pure computation kernels."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from hft_platform.feature.kernel import (
    LobFeatureKernel,
    SymbolState,
    _top_qty,
    compute_changed_mask,
    compute_ofi_l1_raw,
    extract_l1_qty,
    rust_backend_available,
)

# ---------------------------------------------------------------------------
# rust_backend_available
# ---------------------------------------------------------------------------


class TestRustBackendAvailable:
    def test_returns_bool(self) -> None:
        result = rust_backend_available()
        assert isinstance(result, bool)

    def test_false_when_rust_core_missing(self) -> None:
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", None):
            assert rust_backend_available() is False

    def test_true_when_rust_core_present(self) -> None:
        sentinel = object()
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", sentinel):
            assert rust_backend_available() is True


# ---------------------------------------------------------------------------
# _top_qty
# ---------------------------------------------------------------------------


class TestTopQty:
    def test_none_input(self) -> None:
        assert _top_qty(None) is None

    def test_empty_numpy(self) -> None:
        arr = np.empty((0, 2), dtype=np.int64)
        assert _top_qty(arr) == 0

    def test_numpy_array(self) -> None:
        arr = np.array([[100_0000, 50], [99_0000, 30]], dtype=np.int64)
        assert _top_qty(arr) == 50

    def test_list_input(self) -> None:
        book = [[100_0000, 42], [99_0000, 10]]
        assert _top_qty(book) == 42

    def test_empty_list(self) -> None:
        assert _top_qty([]) == 0

    def test_list_single_element_row(self) -> None:
        """Row with only price, no qty."""
        assert _top_qty([[100_0000]]) == 0


# ---------------------------------------------------------------------------
# extract_l1_qty
# ---------------------------------------------------------------------------


class _FakeEvent:
    def __init__(self, bids: object, asks: object) -> None:
        self.bids = bids
        self.asks = asks


class TestExtractL1Qty:
    def test_none_event_uses_fallbacks(self) -> None:
        bq, aq = extract_l1_qty(None, bid_depth_fallback=7, ask_depth_fallback=3)
        assert (bq, aq) == (7, 3)

    def test_negative_fallback_clamped(self) -> None:
        bq, aq = extract_l1_qty(None, bid_depth_fallback=-5, ask_depth_fallback=-1)
        assert (bq, aq) == (0, 0)

    def test_normal_event(self) -> None:
        ev = _FakeEvent(
            bids=np.array([[100_0000, 20], [99_0000, 10]], dtype=np.int64),
            asks=np.array([[101_0000, 15], [102_0000, 5]], dtype=np.int64),
        )
        bq, aq = extract_l1_qty(ev, bid_depth_fallback=0, ask_depth_fallback=0)
        assert (bq, aq) == (20, 15)

    def test_missing_bids_uses_fallback(self) -> None:
        ev = _FakeEvent(bids=None, asks=np.array([[101_0000, 8]], dtype=np.int64))
        bq, aq = extract_l1_qty(ev, bid_depth_fallback=5, ask_depth_fallback=0)
        assert (bq, aq) == (5, 8)


# ---------------------------------------------------------------------------
# compute_ofi_l1_raw — 9 combos (bid up/same/down x ask up/same/down)
# ---------------------------------------------------------------------------


class TestComputeOfiL1Raw:
    """All 9 price-movement combinations for L1 OFI."""

    # Bid up → b_flow = bid_qty
    def test_bid_up_ask_up(self) -> None:
        # bid up: b_flow=100, ask up: a_flow=-50 → OFI = 100-(-50) = 150
        assert compute_ofi_l1_raw(11, 21, 100, 60, 10, 20, 50, 50) == 150

    def test_bid_up_ask_same(self) -> None:
        # bid up: b_flow=100, ask same: a_flow=60-50=10 → OFI = 100-10 = 90
        assert compute_ofi_l1_raw(11, 20, 100, 60, 10, 20, 50, 50) == 90

    def test_bid_up_ask_down(self) -> None:
        # bid up: b_flow=100, ask down: a_flow=60 → OFI = 100-60 = 40
        assert compute_ofi_l1_raw(11, 19, 100, 60, 10, 20, 50, 50) == 40

    # Bid same → b_flow = bid_qty - prev_bid_qty
    def test_bid_same_ask_up(self) -> None:
        # bid same: b_flow=100-50=50, ask up: a_flow=-50 → OFI = 50-(-50) = 100
        assert compute_ofi_l1_raw(10, 21, 100, 60, 10, 20, 50, 50) == 100

    def test_bid_same_ask_same(self) -> None:
        # bid same: b_flow=100-50=50, ask same: a_flow=60-50=10 → OFI = 50-10 = 40
        assert compute_ofi_l1_raw(10, 20, 100, 60, 10, 20, 50, 50) == 40

    def test_bid_same_ask_down(self) -> None:
        # bid same: b_flow=100-50=50, ask down: a_flow=60 → OFI = 50-60 = -10
        assert compute_ofi_l1_raw(10, 19, 100, 60, 10, 20, 50, 50) == -10

    # Bid down → b_flow = -prev_bid_qty
    def test_bid_down_ask_up(self) -> None:
        # bid down: b_flow=-50, ask up: a_flow=-50 → OFI = -50-(-50) = 0
        assert compute_ofi_l1_raw(9, 21, 100, 60, 10, 20, 50, 50) == 0

    def test_bid_down_ask_same(self) -> None:
        # bid down: b_flow=-50, ask same: a_flow=60-50=10 → OFI = -50-10 = -60
        assert compute_ofi_l1_raw(9, 20, 100, 60, 10, 20, 50, 50) == -60

    def test_bid_down_ask_down(self) -> None:
        # bid down: b_flow=-50, ask down: a_flow=60 → OFI = -50-60 = -110
        assert compute_ofi_l1_raw(9, 19, 100, 60, 10, 20, 50, 50) == -110


# ---------------------------------------------------------------------------
# compute_changed_mask
# ---------------------------------------------------------------------------


class TestComputeChangedMask:
    def test_none_prev_all_changed(self) -> None:
        mask = compute_changed_mask(None, (1, 2, 3))
        assert mask == 0b111

    def test_no_change(self) -> None:
        assert compute_changed_mask((5, 10), (5, 10)) == 0

    def test_first_changed(self) -> None:
        assert compute_changed_mask((5, 10), (6, 10)) == 0b01

    def test_second_changed(self) -> None:
        assert compute_changed_mask((5, 10), (5, 11)) == 0b10

    def test_length_mismatch_all_set(self) -> None:
        mask = compute_changed_mask((1, 2), (1, 2, 3))
        assert mask == 0b111

    def test_empty_new_values(self) -> None:
        assert compute_changed_mask(None, ()) == 0


# ---------------------------------------------------------------------------
# SymbolState
# ---------------------------------------------------------------------------


class TestSymbolState:
    def test_default_init(self) -> None:
        s = SymbolState()
        assert s.seq == 0
        assert s.values == ()
        assert s.warm_count == 0
        assert s.quality_flags == 0
        assert s.initialized is False

    def test_update_output(self) -> None:
        s = SymbolState()
        s.update_output(
            seq=5,
            source_ts_ns=1000,
            local_ts_ns=2000,
            values=(1, 2, 3),
            warm_count=10,
            quality_flags=0xFF,
        )
        assert s.seq == 5
        assert s.source_ts_ns == 1000
        assert s.local_ts_ns == 2000
        assert s.values == (1, 2, 3)
        assert s.warm_count == 10
        assert s.quality_flags == 0xFF

    def test_has_slots(self) -> None:
        assert hasattr(SymbolState, "__slots__")


# ---------------------------------------------------------------------------
# LobFeatureKernel.compute — EMA, spread, warmup
# ---------------------------------------------------------------------------


class TestLobFeatureKernel:
    @pytest.fixture()
    def kernel(self) -> LobFeatureKernel:
        return LobFeatureKernel(ema_alpha=0.25, ofi_enabled=True)

    def test_first_tick_initialization(self, kernel: LobFeatureKernel) -> None:
        state = SymbolState()
        vals = kernel.compute(
            state,
            bb=100_0000,
            ba=101_0000,
            mid=100_5000,
            spread=1_0000,
            bd=200,
            ad=100,
            l1bq=50,
            l1aq=30,
        )
        assert len(vals) == 16
        assert state.initialized is True
        # On first tick, OFI fields are zero
        assert vals[11] == 0  # ofi_l1_raw
        assert vals[12] == 0  # ofi_l1_cum
        assert vals[13] == 0  # ofi_l1_ema8
        # Spread EMA initialized to spread
        assert vals[14] == 1_0000

    def test_spread_ema_converges(self, kernel: LobFeatureKernel) -> None:
        """Repeated constant spread → EMA converges to that spread."""
        state = SymbolState()
        for _ in range(50):
            vals = kernel.compute(
                state,
                bb=100_0000,
                ba=101_0000,
                mid=100_5000,
                spread=2_0000,
                bd=100,
                ad=100,
                l1bq=50,
                l1aq=50,
            )
        assert vals[14] == 2_0000  # spread_ema8 converged

    def test_imbalance_ppm_symmetric(self, kernel: LobFeatureKernel) -> None:
        state = SymbolState()
        vals = kernel.compute(
            state,
            bb=100_0000,
            ba=101_0000,
            mid=100_5000,
            spread=1_0000,
            bd=100,
            ad=100,
            l1bq=50,
            l1aq=50,
        )
        assert vals[6] == 0  # depth imbalance ppm
        assert vals[10] == 0  # l1 imbalance ppm

    def test_microprice_skew(self, kernel: LobFeatureKernel) -> None:
        """More ask qty → microprice skews toward bid."""
        state = SymbolState()
        vals = kernel.compute(
            state,
            bb=100_0000,
            ba=102_0000,
            mid=101_0000,
            spread=2_0000,
            bd=100,
            ad=100,
            l1bq=10,
            l1aq=90,
        )
        microprice_x2 = vals[7]
        # microprice_x2 = 2*(ba*l1bq + bb*l1aq)/(l1bq+l1aq)
        expected = int(round(2.0 * ((102_0000 * 10) + (100_0000 * 90)) / 100.0))
        assert microprice_x2 == expected

    def test_ofi_disabled(self) -> None:
        kernel = LobFeatureKernel(ema_alpha=0.25, ofi_enabled=False)
        state = SymbolState()
        # First tick (init)
        kernel.compute(state, 100, 101, 100, 1, 50, 50, 10, 10)
        # Second tick
        vals = kernel.compute(state, 101, 102, 101, 1, 50, 50, 10, 10)
        assert vals[11] == 0  # ofi_l1_raw
        assert vals[12] == 0  # ofi_l1_cum
        assert vals[13] == 0  # ofi_l1_ema8

    def test_depth_zero_imbalance_zero(self, kernel: LobFeatureKernel) -> None:
        state = SymbolState()
        vals = kernel.compute(
            state,
            bb=100,
            ba=101,
            mid=100,
            spread=1,
            bd=0,
            ad=0,
            l1bq=0,
            l1aq=0,
        )
        assert vals[6] == 0  # imbalance_ppm
        assert vals[10] == 0  # l1_imbalance_ppm
        assert vals[7] == 100  # microprice falls back to mid
