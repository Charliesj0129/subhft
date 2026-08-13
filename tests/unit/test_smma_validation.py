from __future__ import annotations

import numpy as np

from research.combinatorial.smma_validation import (
    benjamini_hochberg,
    block_bootstrap_mean,
    build_split_plan,
    evaluate_recent_kill_criteria,
    forward_target_indices,
    monotonic_horizon_pollution,
    simulate_next_bar_execution,
)


def test_forward_labels_never_cross_split_or_reset() -> None:
    days = np.repeat([f"2026-07-{day:02d}" for day in range(1, 21)], 2)
    plan = build_split_plan(days)
    resets = np.zeros(days.size, dtype=bool)
    resets[7] = True
    targets = forward_target_indices(
        horizon="4h",
        timeframe_min=120,
        split_labels=plan.labels,
        reset_mask=resets,
        session_close=np.zeros(days.size, dtype=bool),
    )
    valid = np.flatnonzero(targets >= 0)
    assert np.all(plan.labels[valid] == plan.labels[targets[valid]])
    assert targets[5] == -1
    assert targets[6] == -1


def test_execution_crosses_bid_ask_on_next_bar_and_charges_profile_cost() -> None:
    result = simulate_next_bar_execution(
        signal=np.asarray([2.0, 0.0, 0.0, 0.0]),
        direction=1,
        threshold=1.0,
        target_indices=np.asarray([3, -1, -1, -1]),
        bid_open=np.asarray([99.0, 100.0, 104.0, 105.0]),
        ask_open=np.asarray([101.0, 102.0, 106.0, 107.0]),
        bid_close=np.asarray([100.0, 103.0, 106.0, 108.0]),
        ask_close=np.asarray([102.0, 105.0, 108.0, 110.0]),
        reset_mask=np.zeros(4, dtype=bool),
        instrument_profile="TXFD6",
    )
    assert result.entry_indices.tolist() == [1]
    assert result.exit_indices.tolist() == [3]
    assert result.trade_pnl.tolist() == [3.0]  # 108 close bid - 102 open ask - 3 point RT profile


def test_short_execution_sells_bid_and_covers_ask() -> None:
    result = simulate_next_bar_execution(
        signal=np.asarray([-2.0, 0.0, 0.0, 0.0]),
        direction=-1,
        threshold=1.0,
        target_indices=np.asarray([3, -1, -1, -1]),
        bid_open=np.asarray([101.0, 100.0, 96.0, 94.0]),
        ask_open=np.asarray([103.0, 102.0, 98.0, 95.0]),
        bid_close=np.asarray([100.0, 98.0, 95.0, 91.0]),
        ask_close=np.asarray([102.0, 100.0, 97.0, 92.0]),
        reset_mask=np.zeros(4, dtype=bool),
        instrument_profile="TMFD6",
    )
    assert result.entry_indices.tolist() == [1]
    assert result.exit_indices.tolist() == [3]
    assert result.trade_pnl.tolist() == [4.0]  # 100 open bid - 92 close ask - 4 point RT profile


def test_execution_forces_flat_before_reset() -> None:
    result = simulate_next_bar_execution(
        signal=np.asarray([2.0, 0.0, 0.0, 0.0, 0.0]),
        direction=1,
        threshold=1.0,
        target_indices=np.asarray([4, -1, -1, -1, -1]),
        bid_open=np.asarray([99.0, 100.0, 103.0, 104.0, 105.0]),
        ask_open=np.asarray([101.0, 102.0, 105.0, 106.0, 107.0]),
        bid_close=np.asarray([100.0, 103.0, 110.0, 105.0, 106.0]),
        ask_close=np.asarray([102.0, 105.0, 112.0, 107.0, 108.0]),
        reset_mask=np.asarray([False, False, False, True, False]),
        instrument_profile="TMFD6",
    )
    assert result.exit_indices.tolist() == [2]
    assert result.trade_pnl.tolist() == [4.0]  # 110 close bid - 102 open ask - 4 point RT profile


def test_recent_kill_rejects_constant_or_trend_contaminated_signal() -> None:
    signal = np.arange(100, dtype=float)
    target = np.arange(100, dtype=float)
    execution = simulate_next_bar_execution(
        signal=np.zeros(100),
        direction=1,
        threshold=0.0,
        target_indices=np.full(100, -1),
        bid_open=np.ones(100),
        ask_open=np.ones(100),
        bid_close=np.ones(100),
        ask_close=np.ones(100),
        reset_mask=np.zeros(100, dtype=bool),
        instrument_profile="TXFD6",
    )
    metrics = evaluate_recent_kill_criteria(
        signal=signal,
        target_returns=target,
        execution=execution,
        root="TXF",
        nonoverlap_step=4,
    )
    assert not metrics.passed
    assert "raw_ic_inflation" in metrics.reasons or "detrended_ic" in metrics.reasons


def test_monotonic_horizon_ic_is_killed() -> None:
    from research.combinatorial.smma_validation import KillMetrics

    def metrics(ic: float) -> KillMetrics:
        return KillMetrics(0.0, ic, 0.0, 1.0, 20.0, 1.0, 0.1, -10.0, True, ())

    assert monotonic_horizon_pollution({"1h": metrics(0.02), "4h": metrics(0.03), "session": metrics(0.04)})


def test_multiple_testing_and_bootstrap_are_deterministic() -> None:
    mask = benjamini_hochberg([0.001, 0.02, 0.8], q=0.10)
    assert mask.tolist() == [True, True, False]
    values = np.asarray([1.0, 2.0, 3.0, 4.0])
    assert block_bootstrap_mean(values, samples=100, seed=7) == block_bootstrap_mean(
        values,
        samples=100,
        seed=7,
    )
