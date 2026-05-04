"""Tests for hft_platform.alpha._resampling primitives."""
from __future__ import annotations

import math

import numpy as np
import pytest

from hft_platform.alpha._resampling import (
    day_bootstrap,
    drop_top_trades,
    leave_one_day_out,
    stationary_block_bootstrap,
)


class TestLeaveOneDayOut:
    def test_drops_each_day_once(self) -> None:
        daily = [1.0, 2.0, 3.0]
        out = list(leave_one_day_out(daily))
        assert len(out) == 3
        assert sorted(sum(s) for s in out) == [3.0, 4.0, 5.0]

    def test_empty_input_returns_empty(self) -> None:
        assert list(leave_one_day_out([])) == []

    def test_single_day_yields_one_empty_slice(self) -> None:
        out = list(leave_one_day_out([5.0]))
        assert len(out) == 1 and list(out[0]) == []


class TestDropTopTrades:
    def test_drops_top_pct_by_magnitude(self) -> None:
        trades = [10.0, -50.0, 1.0, 2.0, 3.0]  # |.| sorted desc => -50, 10, 3, 2, 1
        kept = drop_top_trades(trades, pct=0.4)  # drop top 2
        assert sorted(kept) == [1.0, 2.0, 3.0]

    def test_zero_pct_keeps_all(self) -> None:
        assert drop_top_trades([1.0, 2.0, 3.0], pct=0.0) == [1.0, 2.0, 3.0]

    def test_pct_clamped_below_one(self) -> None:
        with pytest.raises(ValueError, match="pct must be in"):
            drop_top_trades([1.0], pct=1.5)

    def test_raises_for_negative_pct(self) -> None:
        with pytest.raises(ValueError, match="pct must be in"):
            drop_top_trades([1.0, 2.0], pct=-0.1)


class TestDayBootstrap:
    def test_returns_n_resamples_of_correct_length(self) -> None:
        daily = [1.0, 2.0, 3.0, 4.0]
        samples = day_bootstrap(daily, n_resamples=100, rng_seed=42)
        assert samples.shape == (100, 4)

    def test_ci_lower_above_threshold_for_clearly_positive(self) -> None:
        rng = np.random.default_rng(0)
        daily = (rng.normal(loc=10.0, scale=1.0, size=100)).tolist()
        samples = day_bootstrap(daily, n_resamples=2000, rng_seed=42)
        means = samples.mean(axis=1)
        ci_low = float(np.quantile(means, 0.05))
        assert ci_low > 5.0

    def test_raises_when_too_few_days(self) -> None:
        with pytest.raises(ValueError, match="insufficient"):
            day_bootstrap([1.0], n_resamples=10, rng_seed=42)

    def test_reproducible_under_same_seed(self) -> None:
        daily = [1.0, 2.0, 3.0, 4.0, 5.0]
        s1 = day_bootstrap(daily, n_resamples=10, rng_seed=99)
        s2 = day_bootstrap(daily, n_resamples=10, rng_seed=99)
        assert np.array_equal(s1, s2)


class TestStationaryBlockBootstrap:
    def test_returns_n_resamples_of_correct_length(self) -> None:
        daily = [float(i) for i in range(50)]
        samples = stationary_block_bootstrap(
            daily, block_size=5, n_resamples=200, rng_seed=42
        )
        assert samples.shape == (200, 50)

    def test_preserves_first_moment_in_expectation(self) -> None:
        daily = [1.0] * 100
        samples = stationary_block_bootstrap(
            daily, block_size=5, n_resamples=500, rng_seed=42
        )
        assert math.isclose(samples.mean(), 1.0, abs_tol=1e-6)

    def test_raises_when_block_size_too_small(self) -> None:
        with pytest.raises(ValueError, match="block_size"):
            stationary_block_bootstrap(
                [1.0, 2.0], block_size=0, n_resamples=10, rng_seed=42
            )

    def test_reproducible_under_same_seed(self) -> None:
        daily = [float(i) for i in range(20)]
        s1 = stationary_block_bootstrap(
            daily, block_size=4, n_resamples=10, rng_seed=99
        )
        s2 = stationary_block_bootstrap(
            daily, block_size=4, n_resamples=10, rng_seed=99
        )
        assert np.array_equal(s1, s2)

    def test_raises_when_input_shorter_than_block_size(self) -> None:
        with pytest.raises(ValueError, match="< block_size"):
            stationary_block_bootstrap(
                [1.0, 2.0], block_size=5, n_resamples=10, rng_seed=42
            )
