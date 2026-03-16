"""Tests for ofi_momentum alpha — OFI MACD signal."""
from __future__ import annotations

import numpy as np
import pytest

from research.alphas.ofi_momentum.impl import (
    ALPHA_CLASS,
    OfiMomentumAlpha,
    _EMA_FAST,
    _EMA_SLOW,
    _MANIFEST,
)


class TestManifest:
    def test_alpha_id(self):
        assert _MANIFEST.alpha_id == "ofi_momentum"

    def test_data_fields(self):
        assert set(_MANIFEST.data_fields) == {"bid_qty", "ask_qty"}

    def test_complexity(self):
        assert _MANIFEST.complexity == "O(1)"

    def test_paper_ref(self):
        assert "0906.1444" in _MANIFEST.paper_refs

    def test_latency_profile(self):
        assert _MANIFEST.latency_profile is not None

    def test_feature_set_version(self):
        assert _MANIFEST.feature_set_version == "lob_shared_v1"

    def test_alpha_class_export(self):
        assert ALPHA_CLASS is OfiMomentumAlpha


class TestProtocol:
    def test_has_required_methods(self):
        a = OfiMomentumAlpha()
        assert callable(a.update)
        assert callable(a.reset)
        assert callable(a.get_signal)
        assert a.manifest is _MANIFEST

    def test_slots(self):
        assert hasattr(OfiMomentumAlpha, "__slots__")


class TestSignalLogic:
    def test_first_tick_zero(self):
        a = OfiMomentumAlpha()
        assert a.update(bid_qty=100, ask_qty=50) == 0.0

    def test_positive_bid_gives_positive(self):
        a = OfiMomentumAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=120, ask_qty=100)
        assert sig > 0

    def test_positive_ask_gives_negative(self):
        a = OfiMomentumAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=120)
        assert sig < 0

    def test_acceleration_effect(self):
        """Sudden increase in OFI should create acceleration spike."""
        a = OfiMomentumAlpha()
        a.update(bid_qty=100, ask_qty=100)
        # Small positive flow for a while
        for _ in range(50):
            a.update(bid_qty=101, ask_qty=100)
            a._prev_bid_qty = 100.0
            a._prev_ask_qty = 100.0
        sig_before = a.get_signal()
        # Sudden large positive flow
        a.update(bid_qty=200, ask_qty=100)
        sig_after = a.get_signal()
        # After spike, fast EMA jumps more than slow → positive acceleration
        assert sig_after > sig_before

    def test_symmetric_change_zero(self):
        a = OfiMomentumAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=110, ask_qty=110)
        assert sig == 0.0

    def test_signal_bounded(self):
        a = OfiMomentumAlpha()
        a.update(bid_qty=100, ask_qty=100)
        for _ in range(100):
            a.update(bid_qty=1000, ask_qty=0)
        assert -2.0 < a.get_signal() < 2.0

    def test_no_change_decays(self):
        a = OfiMomentumAlpha()
        a.update(bid_qty=100, ask_qty=100)
        a.update(bid_qty=120, ask_qty=100)
        for _ in range(500):
            a.update(bid_qty=120, ask_qty=100)
        assert abs(a.get_signal()) < 0.001

    def test_reset(self):
        a = OfiMomentumAlpha()
        a.update(bid_qty=100, ask_qty=50)
        a.update(bid_qty=200, ask_qty=50)
        assert a.get_signal() != 0.0
        a.reset()
        assert a.get_signal() == 0.0

    def test_differs_from_plain_ofi(self):
        """With acceleration, signal should differ from slow-only EMA."""
        a = OfiMomentumAlpha()
        a.update(bid_qty=100, ask_qty=100)
        # Increasing flow over time
        for i in range(20):
            a.update(bid_qty=100 + i * 5, ask_qty=100)
            a._prev_bid_qty = 100 + i * 5 - 5
            a._prev_ask_qty = 100.0
        sig_momentum = a.get_signal()
        # The acceleration component should make this differ from pure level
        # (signal = 0.5*level + 0.5*accel, not just level)
        assert sig_momentum != 0.0


class TestInputInterface:
    def test_positional(self):
        assert OfiMomentumAlpha().update(100, 50) == 0.0

    def test_keyword(self):
        assert OfiMomentumAlpha().update(bid_qty=100, ask_qty=50) == 0.0

    def test_arrays(self):
        bids = np.array([[100.0, 50.0]])
        asks = np.array([[101.0, 30.0]])
        assert OfiMomentumAlpha().update(bids=bids, asks=asks) == 0.0


class TestAntiLeak:
    def test_no_future_data(self):
        a1 = OfiMomentumAlpha()
        a1.update(100, 100)
        s1 = a1.update(110, 100)
        a2 = OfiMomentumAlpha()
        a2.update(100, 100)
        s2 = a2.update(110, 100)
        assert s1 == s2

    def test_change_based(self):
        a1 = OfiMomentumAlpha()
        a1.update(100, 100)
        s1 = a1.update(120, 105)
        a2 = OfiMomentumAlpha()
        a2.update(500, 500)
        s2 = a2.update(520, 505)
        assert s1 == pytest.approx(s2, rel=1e-10)
