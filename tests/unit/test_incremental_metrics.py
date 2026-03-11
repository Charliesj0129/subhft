from __future__ import annotations

import numpy as np
import pytest

from research.backtest.metrics import (
    ICChunkState,
    compute_ic,
    compute_ic_incremental,
    compute_max_drawdown,
    compute_metrics_incremental,
    compute_sharpe,
    compute_turnover,
)
from research.registry.scorecard import (
    _max_pool_correlation,
    compute_pool_correlation_matrix,
)


# ---------------------------------------------------------------------------
# Incremental IC
# ---------------------------------------------------------------------------

class TestComputeICIncremental:
    """Tests for compute_ic_incremental."""

    def test_matches_compute_ic_without_prev_state(self) -> None:
        rng = np.random.default_rng(42)
        signals = rng.standard_normal(200)
        fwd = signals * 0.5 + rng.standard_normal(200) * 0.1

        ic_mean, ic_std, ic_series = compute_ic(signals, fwd, buckets=10)
        inc_mean, inc_std, inc_series, state = compute_ic_incremental(
            signals, fwd, buckets=10, prev_state=None,
        )

        assert state is not None
        np.testing.assert_allclose(inc_mean, ic_mean, atol=1e-12)
        np.testing.assert_allclose(inc_std, ic_std, atol=1e-12)
        np.testing.assert_array_almost_equal(inc_series, ic_series)

    def test_incremental_matches_full_recompute(self) -> None:
        rng = np.random.default_rng(99)
        n_total = 400
        signals = rng.standard_normal(n_total)
        fwd = signals * 0.3 + rng.standard_normal(n_total) * 0.2

        # First half
        half = n_total // 2
        _, _, _, state1 = compute_ic_incremental(
            signals[:half], fwd[:half], buckets=10, prev_state=None,
        )

        # Full window with prev_state from first half
        inc_mean, inc_std, inc_series, state2 = compute_ic_incremental(
            signals, fwd, buckets=10, prev_state=state1,
        )

        # Full window from scratch
        full_mean, full_std, full_series = compute_ic(signals, fwd, buckets=10)

        assert state2 is not None
        np.testing.assert_allclose(inc_mean, full_mean, atol=1e-12)
        np.testing.assert_allclose(inc_std, full_std, atol=1e-12)
        np.testing.assert_array_almost_equal(inc_series, full_series)

    def test_too_few_samples_returns_zeros(self) -> None:
        ic_mean, ic_std, series, state = compute_ic_incremental(
            [1.0, 2.0], [0.5, 0.6], buckets=10,
        )
        assert ic_mean == 0.0
        assert state is None

    def test_state_chunk_size_mismatch_recomputes(self) -> None:
        rng = np.random.default_rng(7)
        signals = rng.standard_normal(100)
        fwd = signals * 0.5 + rng.standard_normal(100) * 0.1

        _, _, _, state_b10 = compute_ic_incremental(
            signals[:50], fwd[:50], buckets=10,
        )

        # Different bucket count changes chunk size -> prev_state discarded
        inc_mean, _, _, state_b5 = compute_ic_incremental(
            signals, fwd, buckets=5, prev_state=state_b10,
        )
        full_mean, _, _ = compute_ic(signals, fwd, buckets=5)
        np.testing.assert_allclose(inc_mean, full_mean, atol=1e-12)


# ---------------------------------------------------------------------------
# Incremental Metrics
# ---------------------------------------------------------------------------

class TestComputeMetricsIncremental:
    """Tests for compute_metrics_incremental."""

    def test_single_slice_sharpe_direction(self) -> None:
        equity = [100.0, 101.0, 102.0, 103.0, 104.0]
        metrics, state = compute_metrics_incremental(None, equity)
        assert metrics["sharpe"] > 0
        assert state.n == 4

    def test_two_slices_approximate_full(self) -> None:
        rng = np.random.default_rng(11)
        equity = np.cumsum(rng.standard_normal(200)) + 1000
        signals = rng.standard_normal(200)
        fwd = signals * 0.2 + rng.standard_normal(200) * 0.1

        half = 100
        m1, state1 = compute_metrics_incremental(
            None, equity[:half], signals[:half], fwd[:half],
        )
        m2, state2 = compute_metrics_incremental(
            state1, equity[half:], signals[half:], fwd[half:],
        )

        # Compare with full-window non-incremental
        full_sharpe = compute_sharpe(equity)
        full_dd = compute_max_drawdown(equity)
        full_turnover = compute_turnover(signals)

        # Sharpe should be in the same ballpark (not exact due to online vs batch variance)
        assert abs(m2["sharpe"] - full_sharpe) / (abs(full_sharpe) + 1e-9) < 0.15
        # Max drawdown should match closely
        assert abs(m2["max_drawdown"] - full_dd) < 0.01
        # Turnover should match exactly
        np.testing.assert_allclose(m2["turnover"], full_turnover, atol=1e-10)

    def test_empty_equity_no_crash(self) -> None:
        metrics, state = compute_metrics_incremental(None, [])
        assert metrics["sharpe"] == 0.0
        assert state.n == 0

    def test_max_drawdown_tracks_across_slices(self) -> None:
        # First slice goes up, second slice has a big drop
        eq1 = [100.0, 110.0, 120.0]
        eq2 = [80.0, 70.0, 130.0]
        _, state1 = compute_metrics_incremental(None, eq1)
        m2, _ = compute_metrics_incremental(state1, eq2)
        # Peak was 120, bottom was 70 → dd = (70-120)/120 = -0.4167
        assert m2["max_drawdown"] < -0.4

    def test_turnover_bridges_slices(self) -> None:
        sig1 = [1.0, 2.0, 3.0]
        sig2 = [5.0, 5.5]
        _, state1 = compute_metrics_incremental(None, [100, 101, 102], sig1)
        m2, _ = compute_metrics_incremental(state1, [103, 104], sig2)
        # Expected diffs: |2-1|=1, |3-2|=1, |5-3|=2, |5.5-5|=0.5 → mean=4.5/4=1.125
        np.testing.assert_allclose(m2["turnover"], 1.125, atol=1e-10)


# ---------------------------------------------------------------------------
# Pool correlation (incremental matrix)
# ---------------------------------------------------------------------------

class TestPoolCorrelationIncremental:
    """Tests for _max_pool_correlation and compute_pool_correlation_matrix."""

    def test_max_pool_correlation_unchanged_without_prev(self) -> None:
        rng = np.random.default_rng(55)
        signal = rng.standard_normal(100)
        pool = {
            "a": rng.standard_normal(100).tolist(),
            "b": rng.standard_normal(100).tolist(),
        }
        result = _max_pool_correlation(signal, pool, prev_matrix=None)
        assert result is not None
        assert 0.0 <= result <= 1.0

    def test_full_matrix_computation(self) -> None:
        rng = np.random.default_rng(22)
        pool = {
            "x": rng.standard_normal(50).tolist(),
            "y": rng.standard_normal(50).tolist(),
        }
        matrix, keys = compute_pool_correlation_matrix(pool)
        assert matrix.shape == (2, 2)
        assert keys == ["x", "y"]
        # Diagonal should be 1.0
        np.testing.assert_allclose(np.diag(matrix), 1.0, atol=1e-10)
        # Symmetric
        np.testing.assert_allclose(matrix, matrix.T, atol=1e-12)

    def test_incremental_matches_full(self) -> None:
        rng = np.random.default_rng(33)
        pool_prev = {
            "a": rng.standard_normal(80).tolist(),
            "b": rng.standard_normal(80).tolist(),
        }
        new_signal = rng.standard_normal(80).tolist()

        # Full computation
        pool_full = {**pool_prev, "c": new_signal}
        full_matrix, full_keys = compute_pool_correlation_matrix(pool_full)

        # Incremental: compute prior matrix, then add "c"
        prev_matrix, prev_keys = compute_pool_correlation_matrix(pool_prev)
        inc_matrix, inc_keys = compute_pool_correlation_matrix(
            pool_full, prev_matrix=prev_matrix, prev_keys=prev_keys,
        )

        assert set(inc_keys) == set(full_keys)
        # Reorder full_matrix to match inc_keys
        idx = [full_keys.index(k) for k in inc_keys]
        full_reordered = full_matrix[np.ix_(idx, idx)]
        np.testing.assert_allclose(inc_matrix, full_reordered, atol=1e-10)

    def test_empty_pool(self) -> None:
        matrix, keys = compute_pool_correlation_matrix({})
        assert matrix.shape == (0, 0)
        assert keys == []

    def test_no_new_alphas_returns_reordered(self) -> None:
        rng = np.random.default_rng(44)
        pool = {
            "a": rng.standard_normal(50).tolist(),
            "b": rng.standard_normal(50).tolist(),
        }
        prev_matrix, prev_keys = compute_pool_correlation_matrix(pool)
        # Same pool, no new keys
        matrix, keys = compute_pool_correlation_matrix(
            pool, prev_matrix=prev_matrix, prev_keys=prev_keys,
        )
        np.testing.assert_allclose(matrix, prev_matrix, atol=1e-12)

    def test_incremental_multiple_new_alphas(self) -> None:
        rng = np.random.default_rng(77)
        pool_prev = {"a": rng.standard_normal(60).tolist()}
        prev_matrix, prev_keys = compute_pool_correlation_matrix(pool_prev)

        pool_full = {
            **pool_prev,
            "b": rng.standard_normal(60).tolist(),
            "c": rng.standard_normal(60).tolist(),
        }
        inc_matrix, inc_keys = compute_pool_correlation_matrix(
            pool_full, prev_matrix=prev_matrix, prev_keys=prev_keys,
        )
        full_matrix, full_keys = compute_pool_correlation_matrix(pool_full)

        idx = [full_keys.index(k) for k in inc_keys]
        full_reordered = full_matrix[np.ix_(idx, idx)]
        np.testing.assert_allclose(inc_matrix, full_reordered, atol=1e-10)
