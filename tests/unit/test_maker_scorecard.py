from __future__ import annotations

import json

from research.backtest.maker_scorecard import (
    MakerPromotionThresholds,
    compute_maker_scorecard,
    evaluate_maker_scorecard,
    load_maker_scorecard,
    save_maker_scorecard,
)


def test_maker_scorecard_aggregates_passive_metrics() -> None:
    scorecard = compute_maker_scorecard(
        [
            {
                "date": "2026-03-19",
                "pnl": 100.0,
                "fills": 4,
                "quotes": 20,
                "cancels": 5,
                "px_chg": 2,
                "max_dd": 10.0,
                "final_pos": 0,
            },
            {
                "date": "2026-03-20",
                "pnl": -20.0,
                "fills": 2,
                "quotes": 10,
                "cancels": 7,
                "px_chg": 3,
                "max_dd": 15.0,
                "final_pos": -1,
            },
        ],
        fills=[
            {"pnl_pts": 0.40, "is_adverse": False, "queue_wait_ms": 12.0},
            {"pnl_pts": -0.10, "is_adverse": True, "queue_wait_ms": 18.0},
            {"pnl_pts": 0.20, "is_adverse": False, "queue_wait_ms": 10.0},
            {"pnl_pts": -0.30, "is_adverse": True, "queue_wait_ms": 30.0},
        ],
        latency_profile={"latency_profile_id": "shioaji_sim_p95", "submit_ack_latency_ms": 36.0},
        config={"symbol": "TXFD6", "queue_model": "PowerProbQueueModel(3.0)"},
    )

    assert scorecard.total_pnl == 80.0
    assert scorecard.total_fills == 6
    assert scorecard.total_quotes == 30
    assert scorecard.pnl_per_fill == 13.333333
    assert scorecard.fill_to_quote_pct == 20.0
    assert scorecard.cancel_to_quote_pct == 40.0
    assert scorecard.price_change_cancel_pct == 16.666667
    assert scorecard.profitable_fill_pct == 50.0
    assert scorecard.adverse_fill_pct == 50.0
    assert scorecard.avg_queue_wait_ms == 17.5
    assert scorecard.winning_day_pct == 50.0
    assert scorecard.max_drawdown == 15.0
    assert scorecard.max_abs_final_inventory == 1
    assert scorecard.latency_profile["latency_profile_id"] == "shioaji_sim_p95"


def test_maker_gate_fails_when_passive_metrics_are_missing_or_unsafe() -> None:
    scorecard = compute_maker_scorecard(
        [
            {
                "date": "2026-03-19",
                "pnl": 20.0,
                "fills": 5,
                "quotes": 10,
                "cancels": 9,
                "max_dd": 1.0,
                "final_pos": 2,
            }
        ]
    )

    decision = evaluate_maker_scorecard(
        scorecard,
        MakerPromotionThresholds(
            min_total_pnl=0.0,
            min_total_fills=1,
            min_profitable_fill_pct=57.0,
            min_winning_day_pct=50.0,
            max_cancel_to_quote_pct=50.0,
            max_abs_final_inventory=0,
            require_latency_profile=True,
        ),
    )

    assert decision.passed is False
    failed = {check.name for check in decision.checks if not check.passed}
    assert failed == {
        "profitable_fill_pct",
        "cancel_to_quote_pct",
        "max_abs_final_inventory",
        "latency_profile_present",
    }


def test_maker_gate_passes_when_thresholds_are_met() -> None:
    scorecard = compute_maker_scorecard(
        [{"date": "2026-03-19", "pnl": 15.0, "fills": 4, "quotes": 20, "cancels": 2, "final_pos": 0}],
        fills=[
            {"pnl_pts": 0.20, "is_adverse": False},
            {"pnl_pts": 0.10, "is_adverse": False},
            {"pnl_pts": 0.05, "is_adverse": False},
            {"pnl_pts": -0.05, "is_adverse": True},
        ],
        latency_profile={"latency_profile_id": "shioaji_sim_p95"},
    )

    decision = evaluate_maker_scorecard(
        scorecard,
        MakerPromotionThresholds(
            min_total_pnl=0.0,
            min_total_fills=4,
            min_profitable_fill_pct=57.0,
            min_winning_day_pct=50.0,
            max_cancel_to_quote_pct=25.0,
            max_abs_final_inventory=0,
            require_latency_profile=True,
        ),
    )

    assert decision.passed is True
    assert all(check.passed for check in decision.checks)


def test_maker_scorecard_round_trips_json(tmp_path) -> None:
    path = tmp_path / "maker_scorecard.json"
    scorecard = compute_maker_scorecard(
        [{"date": "2026-03-19", "pnl": 1.25, "fills": 1, "quotes": 2, "cancels": 0}],
        fills=[{"pnl_pts": 0.25, "is_adverse": False}],
        latency_profile={"latency_profile_id": "sim"},
        config={"symbol": "TXFD6"},
    )

    save_maker_scorecard(path, scorecard)

    payload = json.loads(path.read_text())
    assert payload["schema"] == "maker_scorecard.v1"
    loaded = load_maker_scorecard(path)
    assert loaded.to_dict() == scorecard.to_dict()


def test_maker_scorecard_handles_empty_denominators_without_passing_gate() -> None:
    scorecard = compute_maker_scorecard([])

    assert scorecard.total_fills == 0
    assert scorecard.total_quotes == 0
    assert scorecard.pnl_per_fill is None
    assert scorecard.fill_to_quote_pct is None
    assert scorecard.cancel_to_quote_pct is None
    assert scorecard.profitable_fill_pct is None
    assert scorecard.winning_day_pct is None

    decision = evaluate_maker_scorecard(scorecard)

    assert decision.passed is False
    assert {check.name for check in decision.checks if not check.passed} == {
        "total_fills",
        "profitable_fill_pct",
        "winning_day_pct",
        "cancel_to_quote_pct",
        "latency_profile_present",
    }
