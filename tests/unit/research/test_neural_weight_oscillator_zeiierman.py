from __future__ import annotations

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import Bars
from research.experiments.validations.neural_weight_oscillator_zeiierman_v0.backtest import (
    EvaluationBars,
    Trade,
    beta_neutral_by_stage,
    build_evaluation_bars,
    build_front_month_chain,
    classify_primary_verdict,
    classify_transfer_verdict,
    evaluate_bars,
    evaluate_markets,
    render_expanded_markdown,
    simulate_trades,
    summarize_trades,
    validate_one_contract_per_date,
)
from research.experiments.validations.neural_weight_oscillator_zeiierman_v0.direct_db_bars import (
    build_day_bars_from_rows,
    merge_contract_bars,
)
from research.experiments.validations.neural_weight_oscillator_zeiierman_v0.indicator import (
    IndicatorConfig,
    compute_bwm_weights,
    compute_indicator,
    cross_signals,
)
from research.t1.regime_viability import NS_PER_MINUTE, _session_start_ns


def test_bwm_weights_are_normalized_and_preserve_published_order() -> None:
    weights = compute_bwm_weights(
        best_to_others=(1.0, 3.0, 6.0),
        others_to_worst=(6.0, 3.0, 1.0),
    )

    expected = np.array([6.0, np.sqrt(6.0), 1.0])
    expected /= expected.sum()
    np.testing.assert_allclose(weights, expected)
    assert weights[0] > weights[1] > weights[2]
    assert weights.sum() == 1.0


def test_cross_signals_match_published_alert_semantics() -> None:
    oscillator = np.array([49.0, 52.0, 48.0, 46.0, 51.0])
    signal = np.array([50.0, 50.0, 50.0, 47.0, 50.0])

    long_signal, short_signal = cross_signals(oscillator, signal)

    np.testing.assert_array_equal(long_signal, [False, True, False, False, True])
    np.testing.assert_array_equal(short_signal, [False, False, True, False, False])


def test_online_learning_waits_until_target_is_observable() -> None:
    close = np.linspace(100.0, 150.0, 80)
    config = IndicatorConfig(fast_len=3, slow_len=8, target_len=5, smoothing_len=2, signal_len=3)

    result = compute_indicator(close, close + 1.0, close - 1.0, close, config=config)

    changed = np.flatnonzero(result.learning_updates > 0)
    assert changed.size > 0
    assert changed[0] >= config.slow_len - 1 + config.target_len
    assert np.all(result.learning_updates[: changed[0]] == 0)


def test_appending_future_bars_does_not_change_existing_outputs() -> None:
    rng = np.random.default_rng(20260612)
    close = 20_000.0 + np.cumsum(rng.normal(0.2, 12.0, 240))
    high = close + rng.uniform(1.0, 8.0, close.size)
    low = close - rng.uniform(1.0, 8.0, close.size)
    open_ = np.r_[close[0], close[:-1]]
    config = IndicatorConfig(fast_len=5, slow_len=20, target_len=4)

    prefix = compute_indicator(open_[:160], high[:160], low[:160], close[:160], config=config)
    full = compute_indicator(open_, high, low, close, config=config)

    np.testing.assert_allclose(prefix.oscillator, full.oscillator[:160], equal_nan=True)
    np.testing.assert_allclose(prefix.signal, full.signal[:160], equal_nan=True)
    np.testing.assert_allclose(prefix.learned_weights, full.learned_weights[:160], equal_nan=True)
    np.testing.assert_array_equal(prefix.trigger_long, full.trigger_long[:160])
    np.testing.assert_array_equal(prefix.trigger_short, full.trigger_short[:160])


def _bars(contract: str, dates: list[str]) -> Bars:
    n = len(dates)
    close = np.arange(100.0, 100.0 + n)
    return Bars(
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=np.ones(n),
        date=np.array(dates),
        is_session_close=np.ones(n, dtype=bool),
        contract=contract,
        bid_open=close - 0.5,
        ask_open=close + 0.5,
    )


def test_db_direct_bars_match_frozen_session_and_asof_bbo_semantics() -> None:
    date = "2026-05-21"
    s0 = _session_start_ns(date, hour=8, minute=45)
    first_slot_ts = s0 + np.arange(1, 26) * 5_000_000_000
    second_slot_ts = s0 + 5 * NS_PER_MINUTE + np.arange(1, 26) * 5_000_000_000
    bars = build_day_bars_from_rows(
        tick_ts=np.r_[first_slot_ts, second_slot_ts],
        tick_px=np.r_[np.full(25, 100.0), np.full(25, 101.0)],
        tick_qty=np.ones(50),
        quote_ts=np.array([s0 - NS_PER_MINUTE, s0 + 4 * NS_PER_MINUTE]),
        quote_bid=np.array([99.5, 100.5]),
        quote_ask=np.array([100.5, 101.5]),
        date=date,
        contract="txff6",
        bar_min=5,
        min_bars_per_day=1,
    )

    np.testing.assert_allclose(bars.open, [100.0, 101.0])
    np.testing.assert_allclose(bars.high, [100.0, 101.0])
    np.testing.assert_allclose(bars.low, [100.0, 101.0])
    np.testing.assert_allclose(bars.close, [100.0, 101.0])
    np.testing.assert_allclose(bars.volume, [25.0, 25.0])
    np.testing.assert_allclose(bars.bid_open, [99.5, 100.5])
    np.testing.assert_allclose(bars.ask_open, [100.5, 101.5])
    np.testing.assert_array_equal(bars.is_session_close, [False, True])


def test_merge_contract_bars_sorts_dates_and_rejects_duplicate_days() -> None:
    merged = merge_contract_bars([_bars("txff6", ["2026-06-01"]), _bars("txff6", ["2026-05-21"])])
    assert merged.date.tolist() == ["2026-05-21", "2026-06-01"]

    with np.testing.assert_raises_regex(ValueError, "duplicate dates"):
        merge_contract_bars([_bars("txff6", ["2026-05-21"]), _bars("txff6", ["2026-05-21"])])


def test_evaluation_windows_exclude_overlapping_contract_dates() -> None:
    bars = build_evaluation_bars(
        {
            "development": _bars("txfd6", ["2026-04-15", "2026-04-16"]),
            "primary_oos": _bars("txfe6", ["2026-04-15", "2026-04-16", "2026-05-20", "2026-05-21"]),
            "confirmation_oos": _bars("txff6", ["2026-05-20", "2026-05-21", "2026-06-04"]),
        }
    )

    assert bars.date.tolist() == ["2026-04-15", "2026-04-16", "2026-05-20", "2026-05-21", "2026-06-04"]
    assert bars.stage.tolist() == [
        "development",
        "primary_oos",
        "primary_oos",
        "confirmation_oos",
        "confirmation_oos",
    ]


def test_front_month_chain_uses_frozen_b6_through_f6_windows() -> None:
    bars = build_front_month_chain(
        {
            "b6": _bars("txfb6", ["2026-02-18", "2026-02-19"]),
            "c6": _bars("txfc6", ["2026-02-18", "2026-02-19", "2026-03-18", "2026-03-19"]),
            "d6": _bars("txfd6", ["2026-03-18", "2026-03-19", "2026-04-15", "2026-04-16"]),
            "e6": _bars("txfe6", ["2026-04-15", "2026-04-16", "2026-05-20", "2026-05-21"]),
            "f6": _bars("txff6", ["2026-05-20", "2026-05-21", "2026-06-04", "2026-06-05"]),
        }
    )

    assert bars.date.tolist() == [
        "2026-02-18",
        "2026-02-19",
        "2026-03-18",
        "2026-03-19",
        "2026-04-15",
        "2026-04-16",
        "2026-05-20",
        "2026-05-21",
        "2026-06-04",
    ]
    assert bars.contract.tolist() == [
        "txfb6",
        "txfc6",
        "txfc6",
        "txfd6",
        "txfd6",
        "txfe6",
        "txfe6",
        "txff6",
        "txff6",
    ]
    assert bars.stage.tolist() == [
        "development",
        "development",
        "development",
        "development",
        "development",
        "primary_oos",
        "primary_oos",
        "confirmation_oos",
        "confirmation_oos",
    ]


def test_front_month_validation_rejects_two_contracts_on_same_date() -> None:
    bars = EvaluationBars(
        open=np.array([100.0, 101.0]),
        high=np.array([101.0, 102.0]),
        low=np.array([99.0, 100.0]),
        close=np.array([100.0, 101.0]),
        volume=np.ones(2),
        date=np.array(["2026-04-16", "2026-04-16"]),
        is_session_close=np.ones(2, dtype=bool),
        bid_open=np.array([99.5, 100.5]),
        ask_open=np.array([100.5, 101.5]),
        contract=np.array(["txfd6", "txfe6"]),
        stage=np.array(["primary_oos", "primary_oos"]),
    )

    with np.testing.assert_raises_regex(ValueError, "multiple contracts"):
        validate_one_contract_per_date(bars)


def test_execution_uses_ask_for_buys_bid_for_sells_and_forces_flat() -> None:
    bars = EvaluationBars(
        open=np.array([100.0, 101.0, 102.0, 99.0]),
        high=np.array([101.0, 102.0, 103.0, 100.0]),
        low=np.array([99.0, 100.0, 101.0, 98.0]),
        close=np.array([100.0, 101.0, 102.0, 99.0]),
        volume=np.ones(4),
        date=np.array(["2026-04-16"] * 4),
        is_session_close=np.array([False, False, False, True]),
        bid_open=np.array([99.5, 100.5, 101.5, 98.5]),
        ask_open=np.array([100.5, 101.5, 102.5, 99.5]),
        contract=np.array(["txfe6"] * 4),
        stage=np.array(["primary_oos"] * 4),
    )
    long_signal = np.array([True, False, False, False])
    short_signal = np.array([False, True, False, False])

    result = simulate_trades(
        bars,
        trigger_long=long_signal,
        trigger_short=short_signal,
        regime=np.array(["range", "trend", "trend", "range"]),
        close_half_spread=0.5,
    )

    assert [(trade.side, trade.entry_px, trade.exit_px, trade.exit_reason) for trade in result.trades] == [
        (1, 101.5, 101.5, "flip"),
        (-1, 101.5, 99.5, "session_close"),
    ]
    assert result.bbo_attempts == 2
    assert result.bbo_skipped == 0

    long_only = simulate_trades(
        bars,
        trigger_long=long_signal,
        trigger_short=short_signal,
        regime=np.array(["range", "trend", "trend", "range"]),
        close_half_spread=0.5,
        side="long",
    )
    assert [(trade.side, trade.exit_reason) for trade in long_only.trades] == [(1, "flip")]


def test_missing_next_open_bbo_fails_closed() -> None:
    bars = EvaluationBars(
        open=np.array([100.0, 101.0]),
        high=np.array([101.0, 102.0]),
        low=np.array([99.0, 100.0]),
        close=np.array([100.0, 101.0]),
        volume=np.ones(2),
        date=np.array(["2026-04-16", "2026-04-16"]),
        is_session_close=np.array([False, True]),
        bid_open=np.array([99.5, 100.5]),
        ask_open=np.array([100.5, np.nan]),
        contract=np.array(["txfe6", "txfe6"]),
        stage=np.array(["primary_oos", "primary_oos"]),
    )

    result = simulate_trades(
        bars,
        trigger_long=np.array([True, False]),
        trigger_short=np.array([False, False]),
        regime=np.array(["range", "range"]),
    )

    assert result.trades == []
    assert result.bbo_attempts == 1
    assert result.bbo_skipped == 1


def test_trade_summary_separates_stage_regime_and_extra_cost() -> None:
    trades = [
        Trade(1, 100.0, 101.0, "2026-04-16", "2026-04-16", "primary_oos", "range", "flip"),
        Trade(-1, 102.0, 99.0, "2026-04-17", "2026-04-17", "primary_oos", "trend", "session_close"),
    ]

    summary = summarize_trades(trades, cost_levels=(0.0, 2.0))

    assert summary["all"]["0pt"]["net_total"] == 4.0
    assert summary["all"]["2pt"]["net_total"] == 0.0
    assert summary["by_stage"]["primary_oos"]["0pt"]["n_trades"] == 2
    assert summary["by_regime"]["trend"]["0pt"]["net_total"] == 3.0
    assert summary["by_stage_regime"]["primary_oos"]["trend"]["0pt"]["net_total"] == 3.0
    assert summary["concentration_by_stage"]["primary_oos"]["2pt"]["best_day_loo_total"] == -1.0


def test_beta_neutral_report_is_stage_scoped() -> None:
    close = np.array([100.0, 102.0, 104.0, 103.0])
    bars = EvaluationBars(
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=np.ones(4),
        date=np.array(["2026-04-16"] * 4),
        is_session_close=np.array([False, False, False, True]),
        bid_open=close - 0.5,
        ask_open=close + 0.5,
        contract=np.array(["txfe6"] * 4),
        stage=np.array(["primary_oos"] * 4),
    )
    trades = [
        Trade(
            1,
            100.5,
            102.5,
            "2026-04-16",
            "2026-04-16",
            "primary_oos",
            "trend",
            "session_close",
            entry_bar=1,
            exit_bar=3,
        )
    ]

    report = beta_neutral_by_stage(bars, trades, side="long", n_permutations=50)

    assert report["primary_oos"]["bars_in_position"] == 3
    assert report["primary_oos"]["n_bars"] == 3
    assert report["primary_oos"]["n_trades"] == 1


def test_beta_neutral_excludes_pre_entry_opening_gap() -> None:
    # Position enters at bar 1's open (105) while the prior close is 100 — the
    # 5-pt opening gap must not be credited to the position's first active bar.
    close = np.array([100.0, 110.0, 112.0])
    open_ = np.array([100.0, 105.0, 111.0])
    bars = EvaluationBars(
        open=open_,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=np.ones(3),
        date=np.array(["2026-04-16"] * 3),
        is_session_close=np.array([False, False, True]),
        bid_open=open_ - 0.5,
        ask_open=open_ + 0.5,
        contract=np.array(["txfe6"] * 3),
        stage=np.array(["primary_oos"] * 3),
    )
    trades = [
        Trade(
            1,
            105.0,
            112.0,
            "2026-04-16",
            "2026-04-16",
            "primary_oos",
            "trend",
            "session_close",
            entry_bar=1,
            exit_bar=2,
        )
    ]

    report = beta_neutral_by_stage(bars, trades, side="long", n_permutations=10)

    # Entry bar earns close - open (110 - 105 = 5), continuation bar earns
    # close - close (112 - 110 = 2): mean 3.5, not (10 + 2) / 2 = 6.0.
    assert report["primary_oos"]["strategy_mean_bar_return"] == 3.5
    assert report["primary_oos"]["bars_in_position"] == 2


def test_evaluation_result_exposes_stage_and_execution_evidence() -> None:
    close = np.linspace(100.0, 120.0, 40)
    bars = EvaluationBars(
        open=np.r_[close[0], close[:-1]],
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=np.ones(40),
        date=np.array(["2026-04-16"] * 20 + ["2026-05-21"] * 20),
        is_session_close=np.array([False] * 19 + [True] + [False] * 19 + [True]),
        bid_open=close - 0.5,
        ask_open=close + 0.5,
        contract=np.array(["txfe6"] * 20 + ["txff6"] * 20),
        stage=np.array(["primary_oos"] * 20 + ["confirmation_oos"] * 20),
    )

    result = evaluate_bars(
        bars,
        config=IndicatorConfig(
            fast_len=2,
            slow_len=4,
            smoothing_len=2,
            signal_len=2,
            rsi_len=2,
            mean_len=3,
            momentum_len=2,
            atr_len=2,
            target_len=2,
        ),
    )

    assert result["schema"] == "research.neural_weight_oscillator_zeiierman.v0"
    assert result["fidelity"] == "disclosed_formula_causal_reconstruction"
    assert result["bars_by_stage"] == {"confirmation_oos": 20, "primary_oos": 20}
    assert set(result["execution"]) >= {"bbo_attempts", "bbo_skipped", "bbo_coverage"}
    assert result["contracts_by_stage"] == {
        "confirmation_oos": ["txff6"],
        "primary_oos": ["txfe6"],
    }
    assert set(result["variants"]["long_short"]["beta_neutral"]) == {
        "confirmation_oos",
        "primary_oos",
    }


def _evaluation_stream(close: np.ndarray, market: str) -> EvaluationBars:
    n = len(close)
    split = n // 2
    return EvaluationBars(
        open=np.r_[close[0], close[:-1]],
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=np.ones(n),
        date=np.array(["2026-04-16"] * split + ["2026-05-21"] * (n - split)),
        is_session_close=np.array([False] * (split - 1) + [True] + [False] * (n - split - 1) + [True]),
        bid_open=close - 0.5,
        ask_open=close + 0.5,
        contract=np.array([f"{market}e6"] * split + [f"{market}f6"] * (n - split)),
        stage=np.array(["primary_oos"] * split + ["confirmation_oos"] * (n - split)),
    )


def test_market_evaluation_keeps_txf_and_tmf_learning_state_independent() -> None:
    config = IndicatorConfig(
        fast_len=2,
        slow_len=5,
        smoothing_len=2,
        signal_len=2,
        rsi_len=2,
        mean_len=3,
        momentum_len=2,
        atr_len=2,
        target_len=2,
    )
    txf = _evaluation_stream(np.linspace(100.0, 140.0, 80), "txf")
    tmf = _evaluation_stream(np.linspace(140.0, 95.0, 80), "tmf")

    combined = evaluate_markets({"txf": txf, "tmf": tmf}, config=config)
    standalone_txf = evaluate_bars(txf, config=config)

    assert combined["governance"] == "expanded_retrospective_oos"
    assert combined["markets"]["txf"]["final_learned_weights"] == standalone_txf["final_learned_weights"]
    assert combined["markets"]["txf"]["final_learned_weights"] != combined["markets"]["tmf"]["final_learned_weights"]
    report = render_expanded_markdown(combined)
    assert "expanded_retrospective_oos" in report
    assert f"TXF primary verdict: **{combined['primary_verdict']}**" in report
    assert f"TMF transfer verdict: **{combined['transfer_verdict']}**" in report


def _verdict_result(
    *,
    primary_total: float,
    primary_trades: int,
    primary_loo: float,
    primary_beta: float,
    confirmation_total: float,
    confirmation_trades: int,
    confirmation_loo: float,
    confirmation_beta: float,
) -> dict:
    return {
        "variants": {
            "long_short": {
                "summary": {
                    "by_stage": {
                        "primary_oos": {"2pt": {"net_total": primary_total, "n_trades": primary_trades}},
                        "confirmation_oos": {"2pt": {"net_total": confirmation_total, "n_trades": confirmation_trades}},
                    },
                    "concentration_by_stage": {
                        "primary_oos": {"2pt": {"best_day_loo_total": primary_loo}},
                        "confirmation_oos": {"2pt": {"best_day_loo_total": confirmation_loo}},
                    },
                },
                "beta_neutral": {
                    "primary_oos": {"excess_total_points": primary_beta},
                    "confirmation_oos": {"excess_total_points": confirmation_beta},
                },
            }
        }
    }


def test_expanded_verdict_requires_both_oos_windows_and_robustness() -> None:
    supported = _verdict_result(
        primary_total=100.0,
        primary_trades=12,
        primary_loo=20.0,
        primary_beta=10.0,
        confirmation_total=30.0,
        confirmation_trades=10,
        confirmation_loo=5.0,
        confirmation_beta=2.0,
    )
    concentrated = _verdict_result(
        primary_total=100.0,
        primary_trades=12,
        primary_loo=-1.0,
        primary_beta=10.0,
        confirmation_total=30.0,
        confirmation_trades=10,
        confirmation_loo=5.0,
        confirmation_beta=2.0,
    )
    sparse = _verdict_result(
        primary_total=100.0,
        primary_trades=8,
        primary_loo=20.0,
        primary_beta=10.0,
        confirmation_total=30.0,
        confirmation_trades=4,
        confirmation_loo=5.0,
        confirmation_beta=2.0,
    )

    assert classify_primary_verdict(supported) == "SUPPORTED_RETROSPECTIVELY"
    assert classify_primary_verdict(concentrated) == "NOT_CONFIRMED"
    assert classify_primary_verdict(sparse) == "INSUFFICIENT_SAMPLE"
    assert classify_transfer_verdict(supported) == "transfer_support"
    assert classify_transfer_verdict(concentrated) == "transfer_inconclusive"


def test_negative_transfer_window_is_a_conflict() -> None:
    result = _verdict_result(
        primary_total=10.0,
        primary_trades=12,
        primary_loo=2.0,
        primary_beta=1.0,
        confirmation_total=-1.0,
        confirmation_trades=10,
        confirmation_loo=-3.0,
        confirmation_beta=-2.0,
    )

    assert classify_primary_verdict(result) == "NOT_CONFIRMED"
    assert classify_transfer_verdict(result) == "transfer_conflict"
