"""Unit tests for VRMomentum signal generator."""

from __future__ import annotations

import numpy as np
import pytest

from research.alphas.vr_momentum.impl import VRMomentum, VRRegimeSwitch, _DEFAULT_WARMUP


class TestVRMomentum:

    def test_initial_state(self) -> None:
        vrm = VRMomentum()
        assert not vrm.warmed_up

    def test_no_signal_on_zero(self) -> None:
        vrm = VRMomentum()
        result = vrm.update(0)
        assert result["signal"] == 0

    def test_warmup(self) -> None:
        vrm = VRMomentum(warmup_ticks=100, vr_q=50, push_lag=50)
        for i in range(99):
            vrm.update(200000 + (i % 5) - 2)
        assert not vrm.warmed_up
        for i in range(10):
            vrm.update(200000 + i)
        assert vrm.warmed_up

    def test_trending_gives_high_vr(self) -> None:
        vrm = VRMomentum(vr_q=50, push_lag=50, warmup_ticks=200)
        # Trending: consistent upward moves
        price = 200000
        for i in range(500):
            price += 5
            vrm.update(price)

        result = vrm.update(price + 5)
        vr = result["vr"]
        assert isinstance(vr, float)
        # Trending should give VR > 1
        assert vr > 1.0, f"Trending series should have VR > 1, got {vr}"

    def test_mean_reverting_gives_low_vr(self) -> None:
        vrm = VRMomentum(vr_q=50, push_lag=50, warmup_ticks=200)
        # Mean-reverting: alternating up/down
        price = 200000
        for i in range(500):
            price += 10 if i % 2 == 0 else -10
            vrm.update(price)

        result = vrm.update(price + 5)
        vr = result["vr"]
        # Mean-reverting should give VR < 1
        assert vr < 1.0, f"Mean-reverting series should have VR < 1, got {vr}"

    def test_uptrend_triggers_buy(self) -> None:
        vrm = VRMomentum(
            vr_q=50, push_lag=50, warmup_ticks=200,
            vr_threshold=1.1, z_threshold=1.5,
        )
        price = 200000
        # Warmup with gentle trend
        for i in range(300):
            price += 2
            vrm.update(price)

        # Strong push up
        triggered_buy = False
        for i in range(100):
            price += 20
            result = vrm.update(price)
            if result["signal"] == 1:
                triggered_buy = True
                break

        assert triggered_buy, "Strong uptrend should trigger buy signal"

    def test_downtrend_triggers_sell(self) -> None:
        vrm = VRMomentum(
            vr_q=50, push_lag=50, warmup_ticks=200,
            vr_threshold=1.1, z_threshold=1.5,
        )
        price = 300000
        for i in range(300):
            price -= 2
            vrm.update(price)

        triggered_sell = False
        for i in range(100):
            price -= 20
            price = max(100, price)
            result = vrm.update(price)
            if result["signal"] == -1:
                triggered_sell = True
                break

        assert triggered_sell, "Strong downtrend should trigger sell signal"

    def test_no_signal_when_vr_low(self) -> None:
        vrm = VRMomentum(
            vr_q=50, push_lag=50, warmup_ticks=200,
            vr_threshold=2.0,  # Very high threshold
            z_threshold=1.0,
        )
        rng = np.random.default_rng(42)
        price = 200000
        signals = 0
        for _ in range(1000):
            price += rng.integers(-3, 4)
            price = max(100, price)
            result = vrm.update(price)
            if result["signal"] != 0:
                signals += 1

        assert signals == 0, "High VR threshold should block all signals on random walk"

    def test_reset(self) -> None:
        vrm = VRMomentum(warmup_ticks=100, vr_q=50, push_lag=50)
        for i in range(200):
            vrm.update(200000 + i)
        assert vrm.warmed_up
        vrm.reset()
        assert not vrm.warmed_up

    def test_manifest(self) -> None:
        vrm = VRMomentum()
        m = vrm.manifest
        assert m.alpha_id == "vr_momentum"
        assert "2511.06177" in m.paper_refs

    def test_push_bps_computed(self) -> None:
        vrm = VRMomentum(push_lag=10, vr_q=10, warmup_ticks=50)
        for i in range(100):
            result = vrm.update(200000 + i * 10)
        assert result["push_bps"] != 0.0

    def test_configurable_thresholds(self) -> None:
        vrm = VRMomentum(vr_threshold=1.5, z_threshold=3.0)
        assert vrm.vr_threshold == 1.5
        assert vrm.z_threshold == 3.0

    def test_get_regime_unknown_before_warmup(self) -> None:
        vrm = VRMomentum(warmup_ticks=100, vr_q=50, push_lag=50)
        assert vrm.get_regime() == "unknown"

    def test_get_regime_trending_on_uptrend(self) -> None:
        vrm = VRMomentum(vr_q=50, push_lag=50, warmup_ticks=200)
        price = 200000
        for i in range(500):
            price += 5
            vrm.update(price)
        regime = vrm.get_regime()
        assert regime == "trending"

    def test_get_regime_reverting_on_oscillation(self) -> None:
        vrm = VRMomentum(vr_q=50, push_lag=50, warmup_ticks=200)
        price = 200000
        for i in range(500):
            price += 10 if i % 2 == 0 else -10
            vrm.update(price)
        regime = vrm.get_regime()
        assert regime == "reverting"


class TestVRRegimeSwitch:

    def test_none_before_warmup(self) -> None:
        vrm = VRMomentum(warmup_ticks=100, vr_q=50, push_lag=50)
        switch = VRRegimeSwitch(vrm)
        assert switch.get_active_strategy() == "none"

    def test_vrm_active_on_trend(self) -> None:
        vrm = VRMomentum(vr_q=50, push_lag=50, warmup_ticks=200)
        switch = VRRegimeSwitch(vrm)
        price = 200000
        for i in range(500):
            price += 5
            vrm.update(price)
        assert switch.get_active_strategy() == "vrm"

    def test_cbs_active_on_oscillation(self) -> None:
        vrm = VRMomentum(vr_q=50, push_lag=50, warmup_ticks=200)
        switch = VRRegimeSwitch(vrm)
        price = 200000
        for i in range(500):
            price += 10 if i % 2 == 0 else -10
            vrm.update(price)
        assert switch.get_active_strategy() == "cbs"
