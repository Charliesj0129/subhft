from __future__ import annotations

import numpy as np

from research.combinatorial.smma_validation import (
    ExecutionResult,
    benjamini_hochberg,
    block_bootstrap_mean,
    build_split_plan,
    effective_test_count,
    evaluate_recent_kill_criteria,
    forward_target_indices,
    locked_validation,
    monotonic_horizon_pollution,
    purged_walk_forward_sharpes,
    resolve_quantile_threshold,
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
        direction=1,
        target_returns=target,
        execution=execution,
        root="TXF",
        nonoverlap_step=4,
    )
    assert not metrics.passed
    assert "raw_ic_inflation" in metrics.reasons or "detrended_ic" in metrics.reasons


def test_quantile_threshold_makes_a_micro_scale_feature_tradeable() -> None:
    signal = np.asarray([0.0001, 0.0002, 0.0003, 0.0004])

    cut = resolve_quantile_threshold(signal, direction=1, quantile=0.50)

    assert cut == 0.0003
    assert np.count_nonzero(signal > cut) == 1


def test_negatively_predictive_signal_survives_ic_when_traded_short() -> None:
    rng = np.random.default_rng(7)
    target = rng.normal(size=200)
    execution = ExecutionResult(
        trade_pnl=np.asarray([8.0, 9.0, 10.0, 11.0]),
        entry_indices=np.asarray([1, 2, 3, 4]),
        exit_indices=np.asarray([1, 2, 3, 4]),
        net_edge=9.5,
        net_sharpe=4.0,
        turnover=0.04,
    )

    metrics = evaluate_recent_kill_criteria(
        signal=-target,
        direction=-1,
        target_returns=target,
        execution=execution,
        root="TXF",
        nonoverlap_step=1,
        recent_fraction=1.0,
    )

    assert metrics.detrended_ic > 0.01
    assert "detrended_ic" not in metrics.reasons


def test_zero_trade_candidate_has_distinct_failure_reason() -> None:
    execution = ExecutionResult(
        trade_pnl=np.asarray([], dtype=np.float64),
        entry_indices=np.asarray([], dtype=np.int64),
        exit_indices=np.asarray([], dtype=np.int64),
        net_edge=0.0,
        net_sharpe=0.0,
        turnover=0.0,
    )

    metrics = evaluate_recent_kill_criteria(
        signal=np.linspace(0.0, 1.0, 100),
        direction=1,
        target_returns=np.linspace(0.0, 1.0, 100),
        execution=execution,
        root="TXF",
        nonoverlap_step=1,
    )

    assert "no_trades" in metrics.reasons
    assert "net_edge" not in metrics.reasons


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


def test_effective_test_count_collapses_perfectly_correlated_signals_to_one() -> None:
    base = np.linspace(-1.0, 1.0, 20)

    assert effective_test_count(np.vstack((base, base, -base))) == 1


def test_effective_test_count_equals_trial_count_for_orthogonal_signals() -> None:
    signals = np.asarray(
        [
            [1.0, -1.0, 1.0, -1.0],
            [1.0, 1.0, -1.0, -1.0],
            [1.0, -1.0, -1.0, 1.0],
        ]
    )

    assert effective_test_count(signals) == 3


def test_deflated_sharpe_does_not_tighten_when_only_raw_trial_count_grows() -> None:
    days = np.repeat([f"d{index:02d}" for index in range(10)], 2)
    pnl = np.repeat(np.linspace(1.0, 2.0, 10), 2)
    execution = ExecutionResult(
        trade_pnl=pnl,
        entry_indices=np.arange(days.size, dtype=np.int64),
        exit_indices=np.arange(days.size, dtype=np.int64),
        net_edge=float(np.mean(pnl)),
        net_sharpe=float(np.mean(pnl) / np.std(pnl, ddof=1)),
        turnover=1.0,
    )
    common = {
        "signal": np.linspace(-1.0, 1.0, days.size),
        "target_returns": np.linspace(-1.0, 1.0, days.size),
        "execution": execution,
        "effective_trials": 3,
        "trial_sharpe_std": 0.2,
        "trading_days": days,
        "validation_days": tuple(dict.fromkeys(days)),
        "seed": 9,
    }

    narrow = locked_validation(actual_trials=100, **common)
    broad = locked_validation(actual_trials=20_000, **common)

    assert narrow.deflated_sharpe == broad.deflated_sharpe
    assert narrow.deflated_sharpe_trials_raw == 100
    assert broad.deflated_sharpe_trials_raw == 20_000


def test_walk_forward_reports_nan_for_inactive_calendar_fold() -> None:
    days = np.asarray(["d1", "d1", "d2", "d2", "d3", "d3", "d4", "d4", "d5", "d5"])
    sharpes = purged_walk_forward_sharpes(
        np.asarray([1.0, 2.0]),
        entry_indices=np.asarray([0, 1]),
        exit_indices=np.asarray([0, 1]),
        trading_days=days,
        validation_days=("d1", "d2", "d3", "d4", "d5"),
        folds=5,
    )

    assert np.isfinite(sharpes[0])
    assert all(np.isnan(value) for value in sharpes[1:])


def test_locked_candidate_with_too_few_active_folds_has_distinct_reason() -> None:
    execution = ExecutionResult(
        trade_pnl=np.asarray([1.0, 2.0]),
        entry_indices=np.asarray([0, 1]),
        exit_indices=np.asarray([0, 1]),
        net_edge=1.5,
        net_sharpe=3.0,
        turnover=0.1,
    )

    locked = locked_validation(
        signal=np.linspace(0.0, 1.0, 10),
        target_returns=np.linspace(0.0, 1.0, 10),
        execution=execution,
        actual_trials=20_000,
        effective_trials=3,
        trial_sharpe_std=0.2,
        trading_days=np.asarray(["d1", "d1", "d2", "d2", "d3", "d3", "d4", "d4", "d5", "d5"]),
        validation_days=("d1", "d2", "d3", "d4", "d5"),
        seed=1,
    )

    assert locked.walk_forward_active_folds < 3
    assert "insufficient_trade_activity" in locked.failure_reasons
    assert locked.deflated_sharpe_trials_raw == 20_000
    assert locked.deflated_sharpe_trials_effective == 3


def test_synthetic_high_signal_control_passes_corrected_locked_harness() -> None:
    days = np.repeat([f"d{index:02d}" for index in range(20)], 2)
    signal = np.linspace(-2.0, 2.0, days.size)
    pnl = np.repeat(np.linspace(1.0, 2.0, 20), 2)
    execution = ExecutionResult(
        trade_pnl=pnl,
        entry_indices=np.arange(days.size, dtype=np.int64),
        exit_indices=np.arange(days.size, dtype=np.int64),
        net_edge=float(np.mean(pnl)),
        net_sharpe=float(np.mean(pnl) / np.std(pnl, ddof=1)),
        turnover=1.0,
    )

    locked = locked_validation(
        signal=signal,
        target_returns=signal,
        execution=execution,
        actual_trials=20_000,
        effective_trials=1,
        trial_sharpe_std=0.2,
        trading_days=days,
        validation_days=tuple(dict.fromkeys(days)),
        seed=11,
    )

    assert locked.passed
    assert all(locked.gate_results.values())


def test_shuffled_null_control_does_not_pass_corrected_locked_harness() -> None:
    rng = np.random.default_rng(13)
    days = np.repeat([f"d{index:02d}" for index in range(20)], 2)
    pnl = rng.normal(size=days.size)
    execution = ExecutionResult(
        trade_pnl=pnl,
        entry_indices=np.arange(days.size, dtype=np.int64),
        exit_indices=np.arange(days.size, dtype=np.int64),
        net_edge=float(np.mean(pnl)),
        net_sharpe=float(np.mean(pnl) / np.std(pnl, ddof=1)),
        turnover=1.0,
    )

    locked = locked_validation(
        signal=rng.normal(size=days.size),
        target_returns=rng.normal(size=days.size),
        execution=execution,
        actual_trials=20_000,
        effective_trials=10,
        trial_sharpe_std=0.2,
        trading_days=days,
        validation_days=tuple(dict.fromkeys(days)),
        seed=13,
    )

    assert not locked.passed
