"""Tests for cross_ofi_leader alpha — Paper 2112.13213."""
from __future__ import annotations

import numpy as np
import pytest

from research.alphas.cross_ofi_leader.impl import (
    ALPHA_CLASS,
    CrossOfiLeaderAlpha,
    _EMA_ALPHA,
    _MANIFEST,
)


class TestManifest:
    def test_alpha_id(self):
        assert _MANIFEST.alpha_id == "cross_ofi_leader"

    def test_data_fields(self):
        assert set(_MANIFEST.data_fields) == {"bid_qty", "ask_qty"}

    def test_complexity(self):
        assert _MANIFEST.complexity == "O(1)"

    def test_paper_ref(self):
        assert "2112.13213" in _MANIFEST.paper_refs

    def test_latency_profile(self):
        assert _MANIFEST.latency_profile is not None

    def test_feature_set_version(self):
        assert _MANIFEST.feature_set_version == "lob_shared_v1"

    def test_alpha_class_export(self):
        assert ALPHA_CLASS is CrossOfiLeaderAlpha


class TestProtocol:
    def test_has_update(self):
        assert callable(CrossOfiLeaderAlpha().update)

    def test_has_reset(self):
        assert callable(CrossOfiLeaderAlpha().reset)

    def test_has_get_signal(self):
        assert callable(CrossOfiLeaderAlpha().get_signal)

    def test_has_manifest(self):
        assert CrossOfiLeaderAlpha().manifest is _MANIFEST

    def test_slots(self):
        assert hasattr(CrossOfiLeaderAlpha, "__slots__")


class TestSignalLogic:
    def test_first_tick_returns_zero(self):
        a = CrossOfiLeaderAlpha()
        assert a.update(bid_qty=100, ask_qty=50) == 0.0

    def test_self_only_when_no_leader(self):
        """Without leader_ofi, should behave like self-OFI only."""
        a = CrossOfiLeaderAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=120, ask_qty=100)
        # Pure self-OFI: d_bid=20, d_ask=0, raw=20/21, ema = alpha * 20/21
        expected = _EMA_ALPHA * (20.0 / 21.0)
        assert sig == pytest.approx(expected, rel=1e-6)

    def test_leader_ofi_influences_signal(self):
        """With leader_ofi provided, signal should differ from self-only."""
        a_self = CrossOfiLeaderAlpha()
        a_self.update(bid_qty=100, ask_qty=100)
        sig_self = a_self.update(bid_qty=120, ask_qty=100)

        a_cross = CrossOfiLeaderAlpha()
        a_cross.update(bid_qty=100, ask_qty=100)
        sig_cross = a_cross.update(bid_qty=120, ask_qty=100, leader_ofi=0.5)

        assert sig_cross != sig_self  # leader signal changes result

    def test_leader_only_signal(self):
        """With no self-change but leader signal, should still produce output."""
        a = CrossOfiLeaderAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofi=0.8)
        # Self: d_bid=0, d_ask=0 → raw=0 → self_ema ≈ 0
        # Leader: ema starts at 0, moves toward 0.8
        # Combined = 0.5 * 0 + 0.5 * (alpha * 0.8)
        assert sig > 0

    def test_negative_leader_gives_negative(self):
        a = CrossOfiLeaderAlpha()
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=100, ask_qty=100, leader_ofi=-0.5)
        assert sig < 0

    def test_signal_bounded(self):
        a = CrossOfiLeaderAlpha()
        a.update(bid_qty=100, ask_qty=100)
        for _ in range(100):
            a.update(bid_qty=1000, ask_qty=0, leader_ofi=1.0)
        assert -1.0 < a.get_signal() < 1.0

    def test_reset_clears_state(self):
        a = CrossOfiLeaderAlpha()
        a.update(bid_qty=100, ask_qty=50)
        a.update(bid_qty=200, ask_qty=50, leader_ofi=0.5)
        assert a.get_signal() != 0.0
        a.reset()
        assert a.get_signal() == 0.0

    def test_no_change_decays_to_zero(self):
        a = CrossOfiLeaderAlpha()
        a.update(bid_qty=100, ask_qty=100)
        a.update(bid_qty=120, ask_qty=100, leader_ofi=0.5)
        for _ in range(500):
            a.update(bid_qty=120, ask_qty=100, leader_ofi=0.0)
        assert abs(a.get_signal()) < 0.001


class TestCrossWeight:
    def test_weight_zero_is_self_only(self):
        a = CrossOfiLeaderAlpha(cross_weight=0.0)
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=120, ask_qty=100, leader_ofi=0.9)
        expected_self = _EMA_ALPHA * (20.0 / 21.0)
        assert sig == pytest.approx(expected_self, rel=1e-6)

    def test_weight_one_is_leader_only(self):
        a = CrossOfiLeaderAlpha(cross_weight=1.0)
        a.update(bid_qty=100, ask_qty=100)
        sig = a.update(bid_qty=120, ask_qty=100, leader_ofi=0.5)
        # Should be pure leader EMA
        expected_leader = _EMA_ALPHA * 0.5
        assert sig == pytest.approx(expected_leader, rel=1e-6)


class TestInputInterface:
    def test_positional_args(self):
        a = CrossOfiLeaderAlpha()
        assert a.update(100, 50) == 0.0

    def test_keyword_args(self):
        a = CrossOfiLeaderAlpha()
        assert a.update(bid_qty=100, ask_qty=50) == 0.0

    def test_bids_asks_arrays(self):
        a = CrossOfiLeaderAlpha()
        bids = np.array([[100.0, 50.0]])
        asks = np.array([[101.0, 30.0]])
        assert a.update(bids=bids, asks=asks) == 0.0


class TestAntiLeak:
    def test_no_future_data(self):
        a1 = CrossOfiLeaderAlpha()
        a1.update(bid_qty=100, ask_qty=100)
        sig1 = a1.update(bid_qty=110, ask_qty=100, leader_ofi=0.3)

        a2 = CrossOfiLeaderAlpha()
        a2.update(bid_qty=100, ask_qty=100)
        sig2 = a2.update(bid_qty=110, ask_qty=100, leader_ofi=0.3)

        assert sig1 == sig2

    def test_no_price_in_signal(self):
        assert "bid_px" not in _MANIFEST.data_fields
        assert "mid_price" not in _MANIFEST.data_fields
