"""Unit tests for IntradayJumpRecovery signal generator."""

from __future__ import annotations

import numpy as np
import pytest

from research.alphas.intraday_jump_recovery.impl import (
    IntradayJumpRecovery,
    _WARMUP_TICKS,
)


class TestIntradayJumpRecovery:

    def test_initial_state(self) -> None:
        ijr = IntradayJumpRecovery()
        assert not ijr.warmed_up

    def test_no_signal_on_zero_price(self) -> None:
        ijr = IntradayJumpRecovery()
        result = ijr.update(0)
        assert result["signal"] == 0

    def test_no_signal_during_warmup(self) -> None:
        ijr = IntradayJumpRecovery(lag_ticks=100)
        price = 200000
        for i in range(min(_WARMUP_TICKS, 1500)):
            result = ijr.update(price + (i % 3) - 1)
            assert result["signal"] == 0

    def test_warmed_up_after_enough_ticks(self) -> None:
        ijr = IntradayJumpRecovery(lag_ticks=100)
        price = 200000
        for i in range(_WARMUP_TICKS + 200):
            ijr.update(price + (i % 5) - 2)
        assert ijr.warmed_up

    def test_large_up_push_triggers_sell(self) -> None:
        ijr = IntradayJumpRecovery(lag_ticks=50, z_threshold=1.5)
        base = 200000
        # Normal ticks
        for i in range(2500):
            ijr.update(base + (i % 5) - 2)

        # Inject a large up-push
        triggered_sell = False
        for i in range(60):
            result = ijr.update(base + 500 + i * 10)
            if result["signal"] == -1:
                triggered_sell = True
                break

        assert triggered_sell, "Large up-push should trigger sell (contrarian)"

    def test_large_down_push_triggers_buy(self) -> None:
        ijr = IntradayJumpRecovery(lag_ticks=50, z_threshold=1.5)
        base = 200000
        for i in range(2500):
            ijr.update(base + (i % 5) - 2)

        triggered_buy = False
        for i in range(60):
            result = ijr.update(base - 500 - i * 10)
            if result["signal"] == 1:
                triggered_buy = True
                break

        assert triggered_buy, "Large down-push should trigger buy (contrarian)"

    def test_asymmetric_sizing(self) -> None:
        ijr = IntradayJumpRecovery(lag_ticks=50, z_threshold=1.5, asymmetry_mult=1.5)
        base = 200000
        for i in range(2500):
            ijr.update(base + (i % 5) - 2)

        # Large down-push → buy with higher multiplier
        for i in range(60):
            result = ijr.update(base - 500 - i * 10)
            if result["signal"] == 1:
                assert result["size_mult"] == 1.5
                break

    def test_no_signal_on_random_walk(self) -> None:
        rng = np.random.default_rng(42)
        ijr = IntradayJumpRecovery(lag_ticks=100, z_threshold=2.5)
        price = 200000
        n_signals = 0

        for _ in range(5000):
            price += rng.integers(-2, 3)
            price = max(1, price)
            result = ijr.update(price)
            if result["signal"] != 0:
                n_signals += 1

        signal_rate = n_signals / 5000
        assert signal_rate < 0.10  # Random walk should trigger rarely

    def test_push_stats_reasonable(self) -> None:
        rng = np.random.default_rng(123)
        ijr = IntradayJumpRecovery(lag_ticks=100)
        price = 200000
        for _ in range(3000):
            price += rng.integers(-5, 6)
            price = max(100, price)
            ijr.update(price)

        stats = ijr.get_push_stats()
        assert stats["std_push"] > 0
        assert stats["threshold_bps"] > 0

    def test_reset_clears_state(self) -> None:
        ijr = IntradayJumpRecovery(lag_ticks=100)
        for i in range(3000):
            ijr.update(200000 + i)
        assert ijr.warmed_up
        ijr.reset()
        assert not ijr.warmed_up

    def test_manifest(self) -> None:
        ijr = IntradayJumpRecovery()
        m = ijr.manifest
        assert m.alpha_id == "intraday_jump_recovery"
        assert "2511.06177" in m.paper_refs
