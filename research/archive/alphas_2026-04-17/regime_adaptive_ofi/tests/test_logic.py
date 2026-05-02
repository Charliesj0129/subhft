"""Unit tests for RegimeAdaptiveOFI signal generator."""

from __future__ import annotations

import numpy as np
import pytest

from research.alphas.regime_adaptive_ofi.impl import (
    Regime,
    RegimeAdaptiveOFI,
    _SPREAD_QUIET_MAX,
    _SPREAD_VOLATILE_MIN,
    _WARMUP_TICKS,
    classify_regimes,
    compute_ema_series,
    compute_forward_returns,
    compute_ofi_series,
)


# ---------------------------------------------------------------------------
# RegimeAdaptiveOFI streaming tests
# ---------------------------------------------------------------------------


class TestRegimeAdaptiveOFI:
    def test_initial_state(self) -> None:
        ra = RegimeAdaptiveOFI()
        assert not ra.warmed_up
        assert ra.regime == Regime.NORMAL

    def test_no_signal_during_warmup(self) -> None:
        ra = RegimeAdaptiveOFI()
        for i in range(_WARMUP_TICKS - 1):
            result = ra.update(
                bid_px=20000.0 + i, ask_px=20004.0 + i,
                bid_qty=10.0, ask_qty=10.0,
                mid_price=20002.0 + i, spread_bps=2.0,
            )
            assert result["signal"] == 0.0

    def test_no_signal_on_zero_price(self) -> None:
        ra = RegimeAdaptiveOFI()
        result = ra.update(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        assert result["signal"] == 0.0

    def test_quiet_regime_on_tight_spread(self) -> None:
        ra = RegimeAdaptiveOFI()
        for _ in range(300):
            ra.update(20000.0, 20004.0, 10.0, 10.0, 20002.0, spread_bps=3.0)
        assert ra.regime == Regime.QUIET

    def test_volatile_regime_on_wide_spread(self) -> None:
        ra = RegimeAdaptiveOFI()
        for _ in range(300):
            ra.update(20000.0, 20040.0, 10.0, 10.0, 20020.0, spread_bps=20.0)
        assert ra.regime == Regime.VOLATILE

    def test_normal_regime_on_medium_spread(self) -> None:
        ra = RegimeAdaptiveOFI()
        for _ in range(300):
            ra.update(20000.0, 20020.0, 10.0, 10.0, 20010.0, spread_bps=10.0)
        assert ra.regime == Regime.NORMAL

    def test_ofi_raw_nonzero_on_price_change(self) -> None:
        ra = RegimeAdaptiveOFI()
        # First tick: establish baseline
        ra.update(20000.0, 20004.0, 10.0, 10.0, 20002.0, 2.0)
        # Second tick: bid price moves up (buyer aggressive)
        result = ra.update(20002.0, 20004.0, 15.0, 10.0, 20003.0, 1.0)
        assert result["ofi_raw"] != 0.0

    def test_reset_clears_state(self) -> None:
        ra = RegimeAdaptiveOFI()
        for _ in range(300):
            ra.update(20000.0, 20004.0, 10.0, 10.0, 20002.0, 3.0)
        assert ra.warmed_up
        ra.reset()
        assert not ra.warmed_up
        assert ra.regime == Regime.NORMAL

    def test_manifest(self) -> None:
        ra = RegimeAdaptiveOFI()
        m = ra.manifest
        assert m.alpha_id == "regime_adaptive_ofi"
        assert "2505.17388" in m.paper_refs

    def test_volatile_regime_blocks_signal(self) -> None:
        """In volatile regime, no signals should be generated."""
        ra = RegimeAdaptiveOFI()
        # Warm up in volatile regime
        for i in range(500):
            result = ra.update(
                bid_px=20000.0 + i * 5, ask_px=20040.0 + i * 5,
                bid_qty=float(10 + i % 20), ask_qty=10.0,
                mid_price=20020.0 + i * 5, spread_bps=20.0,
            )
        # In volatile regime, signal should be suppressed
        assert ra.regime == Regime.VOLATILE
        assert result["direction"] == 0


# ---------------------------------------------------------------------------
# Batch function tests
# ---------------------------------------------------------------------------


class TestComputeOFISeries:
    def _make_data(self, n: int) -> np.ndarray:
        dt = np.dtype([
            ("bid_px", "<f8"), ("ask_px", "<f8"),
            ("bid_qty", "<f8"), ("ask_qty", "<f8"),
            ("mid_price", "<f8"), ("spread_bps", "<f8"),
            ("volume", "<f8"), ("local_ts", "<i8"),
        ])
        data = np.zeros(n, dtype=dt)
        data["bid_px"] = 20000.0
        data["ask_px"] = 20004.0
        data["bid_qty"] = 10.0
        data["ask_qty"] = 10.0
        data["mid_price"] = 20002.0
        data["spread_bps"] = 2.0
        data["local_ts"] = np.arange(n) * int(5.5e8)
        return data

    def test_shape_matches_input(self) -> None:
        data = self._make_data(100)
        ofi = compute_ofi_series(data)
        assert ofi.shape == (100,)

    def test_first_tick_is_zero(self) -> None:
        data = self._make_data(100)
        ofi = compute_ofi_series(data)
        assert ofi[0] == 0.0

    def test_constant_book_gives_zero_ofi(self) -> None:
        data = self._make_data(100)
        ofi = compute_ofi_series(data)
        # All constant => OFI = 0 for all ticks
        assert np.all(ofi == 0.0)

    def test_bid_price_increase_gives_positive_ofi(self) -> None:
        data = self._make_data(10)
        data["bid_px"][5] = 20002.0  # bid moves up
        data["bid_qty"][5] = 15.0
        ofi = compute_ofi_series(data)
        assert ofi[5] > 0  # positive OFI (buyer aggressive)


class TestClassifyRegimes:
    def test_tight_spread_is_quiet(self) -> None:
        spread = np.full(500, 3.0)
        regimes = classify_regimes(spread)
        # After EMA warmup, should be quiet
        assert regimes[-1] == Regime.QUIET

    def test_wide_spread_is_volatile(self) -> None:
        spread = np.full(500, 20.0)
        regimes = classify_regimes(spread)
        assert regimes[-1] == Regime.VOLATILE

    def test_medium_spread_is_normal(self) -> None:
        spread = np.full(500, 10.0)
        regimes = classify_regimes(spread)
        assert regimes[-1] == Regime.NORMAL


class TestComputeEMASeries:
    def test_shape(self) -> None:
        v = np.ones(100)
        ema = compute_ema_series(v, window=10)
        assert ema.shape == (100,)

    def test_converges_to_constant(self) -> None:
        v = np.full(1000, 5.0)
        ema = compute_ema_series(v, window=10)
        assert abs(ema[-1] - 5.0) < 0.1


class TestComputeForwardReturns:
    def test_shape(self) -> None:
        mid = np.linspace(20000, 20100, 1000)
        fwd = compute_forward_returns(mid, 10)
        assert fwd.shape == (1000,)

    def test_last_elements_nan(self) -> None:
        mid = np.linspace(20000, 20100, 1000)
        fwd = compute_forward_returns(mid, 10)
        assert np.all(np.isnan(fwd[-10:]))

    def test_uptrend_positive(self) -> None:
        mid = np.linspace(20000, 21000, 1000)
        fwd = compute_forward_returns(mid, 10)
        assert np.nanmean(fwd) > 0
