"""Edge-case tests for research alpha implementations (R12 coverage).

Tests NaN/inf inputs, empty arrays, reset behaviour, and OFI batch validation.
"""
from __future__ import annotations

import numpy as np
import pytest


# ─── OFIMCAlpha edge cases ────────────────────────────────────────────────────

class TestOFIMCAlphaEdgeCases:
    def _make(self):
        from research.alphas.ofi_mc.impl import OFIMCAlpha
        return OFIMCAlpha()

    def test_first_call_returns_zero(self):
        alpha = self._make()
        sig = alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0, trade_vol=1.0, current_mid=100.5)
        assert sig == pytest.approx(0.0)

    def test_signal_nonzero_after_second_call(self):
        alpha = self._make()
        alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0, trade_vol=1.0, current_mid=100.5)
        sig = alpha.update(bid_px=100.5, bid_qty=12.0, ask_px=101.0, ask_qty=8.0, trade_vol=2.0, current_mid=100.75)
        assert sig != 0.0

    def test_reset_clears_state(self):
        alpha = self._make()
        alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0, trade_vol=5.0, current_mid=100.5)
        alpha.update(bid_px=101.0, bid_qty=12.0, ask_px=102.0, ask_qty=7.0, trade_vol=3.0, current_mid=101.5)
        alpha.reset()
        assert alpha.cumulative_ofi == pytest.approx(0.0)
        assert alpha.cumulative_vol == pytest.approx(0.0)
        assert alpha._signal == pytest.approx(0.0)
        assert alpha.history == []

    def test_zero_trade_vol_handled(self):
        alpha = self._make()
        alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0, trade_vol=0.0, current_mid=100.5)
        # Should not raise, signal = 0.0
        sig = alpha.update(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0, trade_vol=0.0, current_mid=100.5)
        assert np.isfinite(sig)

    def test_update_batch_empty_returns_zeros(self):
        alpha = self._make()
        out = alpha.update_batch(np.zeros(0, dtype=np.float64))
        assert out.size == 0

    def test_update_batch_missing_required_field_raises(self):
        from research.alphas.ofi_mc.impl import OFIMCAlpha
        alpha = OFIMCAlpha()
        # Structured array missing bid_px / best_bid / bid_price entirely
        dt = np.dtype([("close", "f8"), ("volume", "f8")])
        arr = np.zeros(5, dtype=dt)
        with pytest.raises(ValueError, match="Missing required field 'bid_px'"):
            alpha.update_batch(arr)

    def test_update_batch_with_aliases(self):
        alpha = self._make()
        dt = np.dtype([
            ("best_bid", "f8"), ("best_ask", "f8"),
            ("bid_depth", "f8"), ("ask_depth", "f8"),
            ("qty", "f8"), ("mid", "f8"),
        ])
        arr = np.zeros(4, dtype=dt)
        arr["best_bid"] = 100.0
        arr["best_ask"] = 101.0
        arr["bid_depth"] = 10.0
        arr["ask_depth"] = 8.0
        arr["qty"] = 1.0
        arr["mid"] = 100.5
        out = alpha.update_batch(arr)
        assert out.shape == (4,)
        assert np.all(np.isfinite(out))


# ─── OFIMCFactor extreme-value tests ─────────────────────────────────────────

class TestOFIMCFactorExtremes:
    def _make(self):
        from research.alphas.ofi_mc.impl import OFIMCFactor
        return OFIMCFactor()

    def test_large_bid_qty(self):
        factor = self._make()
        factor.compute(bid_px=100.0, bid_qty=1e9, ask_px=101.0, ask_qty=1.0, trade_vol=1.0, current_mid=100.5)
        result = factor.compute(bid_px=101.0, bid_qty=1e9, ask_px=102.0, ask_qty=1.0, trade_vol=1.0, current_mid=101.5)
        assert result is not None
        assert np.isfinite(result.ofi_vol_norm)

    def test_signal_after_reset_is_zero(self):
        factor = self._make()
        factor.compute(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0, trade_vol=1.0, current_mid=100.5)
        factor.compute(bid_px=101.0, bid_qty=12.0, ask_px=102.0, ask_qty=7.0, trade_vol=2.0, current_mid=101.5)
        factor.reset()
        assert factor.get_signal() == pytest.approx(0.0)


# ─── GARCH vol alpha edge cases ───────────────────────────────────────────────

class TestGARCHVolEdgeCases:
    def _make(self):
        from research.alphas.garch_vol.impl import GARCHVolAlpha
        return GARCHVolAlpha()

    def test_manifest_id(self):
        alpha = self._make()
        assert alpha.manifest.alpha_id == "garch_vol"

    def test_reset_clears_variance(self):
        alpha = self._make()
        # GARCHVolAlpha.update() accepts 'price' kwarg
        for price in [100.0, 100.5, 99.8, 100.2]:
            alpha.update(price=price)
        alpha.reset()
        # After reset, _signal is reset to initial_vol (not 0.0)
        assert alpha.get_signal() == pytest.approx(alpha.initial_vol)
        assert alpha.prev_return == pytest.approx(0.0)
        assert alpha.last_price is None

    def test_signal_nonneg_after_updates(self):
        alpha = self._make()
        prices = [100.0, 100.5, 99.8, 100.2, 100.9, 100.1]
        alpha.update(price=prices[0])  # first call returns 0.0
        sig = 0.0
        for price in prices[1:]:
            sig = alpha.update(price=price)
        assert sig >= 0.0  # GARCH vol should be non-negative


# ─── KL Regime alpha edge cases ───────────────────────────────────────────────

class TestKLRegimeEdgeCases:
    def _make(self):
        from research.alphas.kl_regime.impl import KLRegimeAlpha
        return KLRegimeAlpha()

    def test_manifest_id(self):
        alpha = self._make()
        assert alpha.manifest.alpha_id == "kl_regime"

    def test_signal_finite_after_many_updates(self):
        alpha = self._make()
        rng = np.random.default_rng(42)
        # KLRegimeAlpha.update() accepts 'current_return' kwarg
        returns = rng.normal(0, 0.01, 200)
        last_sig = 0.0
        for r in returns:
            last_sig = alpha.update(current_return=float(r))
        assert np.isfinite(last_sig)

    def test_reset_works(self):
        alpha = self._make()
        for _ in range(50):
            alpha.update(current_return=0.01)
        alpha.reset()
        sig = alpha.get_signal()
        assert np.isfinite(sig)
        assert sig == pytest.approx(0.0)


