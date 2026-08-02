from __future__ import annotations

import numpy as np
import pytest

from research.combinatorial.smma_validation import (
    ExecutionResult,
    activation_mask,
    benjamini_hochberg,
    block_bootstrap_mean,
    build_split_plan,
    cluster_permutation_test,
    effective_test_count,
    evaluate_recent_kill_criteria,
    forward_target_indices,
    locked_validation,
    monotonic_horizon_pollution,
    purged_walk_forward_evidence,
    purged_walk_forward_sharpes,
    resolve_quantile_threshold,
    run_locked_harness_controls,
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


def test_execution_uses_entry_contract_profile_and_fails_on_unknown_contract() -> None:
    kwargs = {
        "signal": np.asarray([1.0, 0.0, 0.0]),
        "direction": 1,
        "threshold": 1.0,
        "target_indices": np.asarray([2, -1, -1]),
        "bid_open": np.asarray([99.0, 100.0, 101.0]),
        "ask_open": np.asarray([101.0, 102.0, 103.0]),
        "bid_close": np.asarray([100.0, 103.0, 108.0]),
        "ask_close": np.asarray([102.0, 105.0, 110.0]),
        "reset_mask": np.zeros(3, dtype=bool),
        "cost_mode": "per_contract",
    }
    result = simulate_next_bar_execution(
        **kwargs,
        contracts=np.asarray(["TXFD6", "TXFD6", "TXFD6"]),
    )
    assert result.trade_pnl.tolist() == [3.0]

    with pytest.raises(KeyError, match="TXFZ9"):
        simulate_next_bar_execution(
            **kwargs,
            contracts=np.asarray(["TXFZ9", "TXFZ9", "TXFZ9"]),
        )


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

    resolution = resolve_quantile_threshold(signal, direction=1, quantile=0.50)

    assert resolution.cut == 0.0003
    assert resolution.comparator == ">="
    assert resolution.active_count == 2
    assert np.count_nonzero(activation_mask(signal, direction=1, threshold=resolution.cut)) == 2


def test_quantile_equality_activates_a_discrete_sign_feature() -> None:
    signal = np.asarray([-1.0, 0.0, 1.0, 1.0])
    resolution = resolve_quantile_threshold(signal, direction=1, quantile=0.70)

    assert resolution.cut == 1.0
    assert resolution.tie_count == 2
    assert activation_mask(signal, direction=1, threshold=resolution.cut).tolist() == [
        False,
        False,
        True,
        True,
    ]


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

    assert "no_executable_trades" in metrics.reasons
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


def test_session_block_permutation_is_deterministic_and_breaks_cross_day_pairing() -> None:
    clusters = np.repeat([f"d{index:02d}" for index in range(12)], 4)
    signal = np.arange(clusters.size, dtype=np.float64)

    first = cluster_permutation_test(signal, signal, clusters, samples=199, seed=7)
    second = cluster_permutation_test(signal, signal, clusters, samples=199, seed=7)

    assert first == second
    assert first.pvalue <= 0.10
    assert first.clusters == 12
    assert first.exchangeable_groups == 1
    assert first.reason == "ok"


def test_session_block_permutation_excludes_nonexchangeable_validity_patterns() -> None:
    clusters = np.repeat(["a", "b", "c", "d"], 4)
    signal = np.arange(clusters.size, dtype=np.float64)
    target = signal.copy()
    signal[8] = np.nan
    target[15] = np.nan

    result = cluster_permutation_test(signal, target, clusters, samples=49, seed=3)

    assert result.reason == "insufficient_exchangeable_clusters"
    assert result.clusters == 2
    assert result.excluded_clusters == 2
    assert result.observations == 8


def test_session_block_permutation_rejects_misaligned_cluster_vector() -> None:
    with pytest.raises(ValueError, match="identical lengths"):
        cluster_permutation_test(
            np.arange(8, dtype=np.float64),
            np.arange(8, dtype=np.float64),
            np.asarray(["a"] * 7),
            seed=1,
        )


def test_session_block_permutation_excludes_shifted_time_bucket_signature() -> None:
    clusters = np.repeat(["a", "b", "c"], 4)
    positions = np.concatenate((np.arange(4), np.arange(4), np.arange(1, 5)))
    signal = np.arange(clusters.size, dtype=np.float64)

    result = cluster_permutation_test(
        signal,
        signal,
        clusters,
        positions=positions,
        samples=49,
        seed=2,
    )

    assert result.reason == "insufficient_exchangeable_clusters"
    assert result.clusters == 2
    assert result.excluded_clusters == 1


def test_session_block_permutation_fails_closed_below_frozen_cluster_minimum() -> None:
    clusters = np.repeat(["a", "b", "c", "d"], 4)
    signal = np.arange(clusters.size, dtype=np.float64)

    result = cluster_permutation_test(signal, signal, clusters, samples=199, seed=7)

    assert result.pvalue == 1.0
    assert result.clusters == 4
    assert result.reason == "insufficient_exchangeable_clusters"


def test_session_block_permutation_does_not_count_all_nan_blocks_as_informative() -> None:
    clusters = np.repeat([f"d{index:02d}" for index in range(12)], 4)
    signal = np.arange(clusters.size, dtype=np.float64)
    target = signal.copy()
    signal[-8:] = np.nan
    target[-8:] = np.nan

    result = cluster_permutation_test(signal, target, clusters, samples=49, seed=7)

    assert result.clusters == 10
    assert result.excluded_clusters == 2
    assert result.reason == "ok_excluded_nonexchangeable_blocks"


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
        "feature_history_exact": True,
        "feature_history_bars": 1,
        "signal_history_start_indices": np.arange(days.size, dtype=np.int64),
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


def test_walk_forward_purges_trade_whose_feature_history_crosses_fold_start() -> None:
    days = np.repeat(["d1", "d2", "d3", "d4"], 3)
    entries = np.asarray([1, 4, 7, 9])
    history_starts = np.arange(days.size, dtype=np.int64)
    history_starts[6] = 5  # decision for entry 7 reaches behind fold-2 row 6

    evidence = purged_walk_forward_evidence(
        np.asarray([1.0, 2.0, 3.0, 4.0]),
        entry_indices=entries,
        exit_indices=entries,
        trading_days=days,
        validation_days=("d1", "d2", "d3", "d4"),
        signal_history_start_indices=history_starts,
        folds=2,
    )

    assert evidence.fold_purged_counts == (0, 1)
    assert evidence.fold_trade_counts == (2, 1)
    assert np.isfinite(evidence.sharpes[0])
    assert np.isnan(evidence.sharpes[1])


def test_walk_forward_rejects_future_feature_provenance() -> None:
    with pytest.raises(ValueError, match="same/past"):
        purged_walk_forward_evidence(
            np.asarray([1.0, 2.0]),
            entry_indices=np.asarray([1, 3]),
            exit_indices=np.asarray([1, 3]),
            trading_days=np.repeat(["d1", "d2"], 2),
            validation_days=("d1", "d2"),
            signal_history_start_indices=np.asarray([0, 2, 2, 3]),
            folds=2,
        )


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
        feature_history_exact=True,
        feature_history_bars=1,
        signal_history_start_indices=np.arange(10, dtype=np.int64),
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
        feature_history_exact=True,
        feature_history_bars=1,
        signal_history_start_indices=np.arange(days.size, dtype=np.int64),
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
        feature_history_exact=True,
        feature_history_bars=1,
        signal_history_start_indices=np.arange(days.size, dtype=np.int64),
        seed=13,
    )

    assert not locked.passed


def test_locked_validation_fails_closed_when_feature_history_is_unverified() -> None:
    days = np.repeat([f"d{index:02d}" for index in range(10)], 2)
    pnl = np.linspace(1.0, 2.0, days.size)
    execution = ExecutionResult(
        trade_pnl=pnl,
        entry_indices=np.arange(days.size, dtype=np.int64),
        exit_indices=np.arange(days.size, dtype=np.int64),
        net_edge=float(np.mean(pnl)),
        net_sharpe=1.0,
        turnover=1.0,
    )

    locked = locked_validation(
        signal=np.linspace(-1.0, 1.0, days.size),
        target_returns=np.linspace(-1.0, 1.0, days.size),
        execution=execution,
        actual_trials=100,
        trading_days=days,
        seed=5,
    )

    assert not locked.passed
    assert "feature_history_unverified" in locked.failure_reasons
    assert locked.feature_history_exact is False


def test_locked_validation_rejects_exact_history_claim_without_provenance_vector() -> None:
    days = np.repeat([f"d{index:02d}" for index in range(5)], 2)
    execution = ExecutionResult(
        trade_pnl=np.linspace(1.0, 2.0, days.size),
        entry_indices=np.arange(days.size, dtype=np.int64),
        exit_indices=np.arange(days.size, dtype=np.int64),
        net_edge=1.5,
        net_sharpe=1.0,
        turnover=1.0,
    )

    with pytest.raises(ValueError, match="requires both"):
        locked_validation(
            signal=np.linspace(-1.0, 1.0, days.size),
            target_returns=np.linspace(-1.0, 1.0, days.size),
            execution=execution,
            actual_trials=100,
            trading_days=days,
            feature_history_exact=True,
            feature_history_bars=1,
            seed=5,
        )
    with pytest.raises(ValueError, match="requires calendar"):
        locked_validation(
            signal=np.linspace(-1.0, 1.0, days.size),
            target_returns=np.linspace(-1.0, 1.0, days.size),
            execution=execution,
            actual_trials=100,
            signal_history_start_indices=np.arange(days.size, dtype=np.int64),
            feature_history_exact=True,
            feature_history_bars=1,
            seed=5,
        )
    with pytest.raises(ValueError, match="must be positive"):
        locked_validation(
            signal=np.linspace(-1.0, 1.0, days.size),
            target_returns=np.linspace(-1.0, 1.0, days.size),
            execution=execution,
            actual_trials=100,
            trading_days=days,
            signal_history_start_indices=np.arange(days.size, dtype=np.int64),
            feature_history_exact=True,
            feature_history_bars=0,
            seed=5,
        )


def test_aggregate_harness_controls_meet_frozen_positive_and_null_bounds() -> None:
    summary = run_locked_harness_controls(seed=19, resample_samples=49)

    assert summary.positive_cases == 20
    assert summary.positive_passes >= 18
    assert summary.null_cases == 100
    assert summary.null_survivors <= 10
    assert summary.effective_trials == 20
    assert summary.passed
    assert set(summary.positive_gate_pass_counts) == {
        "cluster_bootstrap",
        "permutation",
        "deflated_sharpe",
        "walk_forward",
    }
    assert len({case.seed for case in summary.cases}) == 120
    seasonal_nulls = [
        case for case in summary.cases if case.scenario == "shared_intraday_seasonality_independent_day_shocks"
    ]
    assert len(seasonal_nulls) == 50
    assert sum(case.gate_results["permutation"] for case in seasonal_nulls) <= 5
    assert summary.interpretation == "conditional_harness_calibration_only_not_alpha_evidence"
