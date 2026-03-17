"""Tests for flow_mode_decomp alpha — Paper 2405.10654."""
from __future__ import annotations

import math

import numpy as np
import pytest

from research.alphas.flow_mode_decomp.impl import (
    ALPHA_CLASS,
    FlowModeDecompAlpha,
    _EMA_ALPHA,
    _MANIFEST,
)

# ---------------------------------------------------------------------------
# Manifest / protocol tests
# ---------------------------------------------------------------------------

class TestManifest:
    def test_alpha_id(self):
        assert _MANIFEST.alpha_id == "flow_mode_decomp"

    def test_data_fields(self):
        assert set(_MANIFEST.data_fields) == {"bid_qty", "ask_qty"}

    def test_complexity(self):
        assert _MANIFEST.complexity == "O(1)"

    def test_paper_ref(self):
        assert "2405.10654" in _MANIFEST.paper_refs

    def test_latency_profile_set(self):
        assert _MANIFEST.latency_profile is not None

    def test_feature_set_version(self):
        assert _MANIFEST.feature_set_version == "lob_shared_v1"

    def test_alpha_class_export(self):
        assert ALPHA_CLASS is FlowModeDecompAlpha


class TestProtocol:
    def test_has_update(self):
        a = FlowModeDecompAlpha()
        assert callable(a.update)

    def test_has_reset(self):
        a = FlowModeDecompAlpha()
        assert callable(a.reset)

    def test_has_get_signal(self):
        a = FlowModeDecompAlpha()
        assert callable(a.get_signal)

    def test_has_manifest(self):
        a = FlowModeDecompAlpha()
        assert a.manifest is _MANIFEST

    def test_slots(self):
        assert hasattr(FlowModeDecompAlpha, "__slots__")


# ---------------------------------------------------------------------------
# Signal logic tests
# ---------------------------------------------------------------------------

class TestSignalLogic:
    def test_first_tick_returns_zero(self):
        a = FlowModeDecompAlpha()
        sig = a.update(bid_qty=100, ask_qty=50)
        assert sig == 0.0

    def test_symmetric_change_gives_zero(self):
        """Equal increase on both sides → A_t = 0 → signal stays near 0."""
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=110, ask_qty=110)
        assert sig == 0.0

    def test_pure_bid_increase_gives_positive(self):
        """Only bid increases → directional pressure is positive."""
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=120, ask_qty=100)
        expected_raw = 20.0 / 21.0
        expected_ema = _EMA_ALPHA * expected_raw
        assert sig == pytest.approx(expected_ema, rel=1e-6)
        assert sig > 0

    def test_pure_ask_increase_gives_negative(self):
        """Only ask increases → directional pressure is negative."""
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=120)
        assert sig < 0

    def test_signal_bounded(self):
        """Signal should stay in (-1, 1) range."""
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=100)
        for _ in range(50):
            a.update(bid_qty=1000, ask_qty=0)
        assert -1.0 < a.get_signal() < 1.0

    def test_no_change_decays_to_zero(self):
        """Constant book → signal decays toward zero."""
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=50)
        a.update(bid_qty=120, ask_qty=50)  # positive shock
        for _ in range(500):
            a.update(bid_qty=120, ask_qty=50)  # no change
        assert abs(a.get_signal()) < 0.001

    def test_reset_clears_state(self):
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=50)
        a.update(bid_qty=200, ask_qty=50)
        assert a.get_signal() != 0.0
        a.reset()
        assert a.get_signal() == 0.0
        sig = a.update(bid_qty=100, ask_qty=100)
        assert sig == 0.0


class TestInputInterface:
    def test_positional_args(self):
        a = FlowModeDecompAlpha()
        sig = a.update(100, 50)
        assert sig == 0.0

    def test_keyword_args(self):
        a = FlowModeDecompAlpha()
        sig = a.update(bid_qty=100, ask_qty=50)
        assert sig == 0.0

    def test_bids_asks_arrays(self):
        a = FlowModeDecompAlpha()
        bids = np.array([[100.0, 50.0]])
        asks = np.array([[101.0, 30.0]])
        sig = a.update(bids=bids, asks=asks)
        assert sig == 0.0


class TestEMAConvergence:
    def test_converges_to_steady_state(self):
        """Constant directional flow → EMA converges to raw value."""
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=100)
        for _ in range(500):
            a.update(bid_qty=110, ask_qty=100)
            a._prev_bid_qty = 100.0
            a._prev_ask_qty = 100.0
        expected = 10.0 / 11.0
        assert a.get_signal() == pytest.approx(expected, rel=0.01)


class TestAntiLeak:
    def test_no_future_data(self):
        """Signal at time t must not use data from t+1."""
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig_t = a.update(bid_qty=110, ask_qty=100)
        sig_t_copy = sig_t
        a_copy = FlowModeDecompAlpha()
        a_copy.update(bid_qty=100, ask_qty=100)
        a_copy.update(bid_qty=110, ask_qty=100)
        assert sig_t_copy == a_copy.get_signal()

    def test_signal_depends_on_changes_not_levels(self):
        """Two sequences with same changes but different levels → same signal."""
        a1 = FlowModeDecompAlpha()
        a1.update(bid_qty=100, ask_qty=100)
        sig1 = a1.update(bid_qty=120, ask_qty=105)

        a2 = FlowModeDecompAlpha()
        a2.update(bid_qty=500, ask_qty=500)
        sig2 = a2.update(bid_qty=520, ask_qty=505)

        assert sig1 == pytest.approx(sig2, rel=1e-10)

    def test_no_price_in_signal(self):
        """Signal must not depend on price data (only quantities)."""
        a = FlowModeDecompAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=110, ask_qty=95)
        assert isinstance(sig, float)
        assert "bid_px" not in _MANIFEST.data_fields
        assert "mid_price" not in _MANIFEST.data_fields
