import pytest

from research.calibration.scoring import DailyFillSummary
from research.calibration.sweep import (
    QueueModelCandidate,
    generate_candidates,
    sweep_exponent,
)


def test_generate_candidates_power_prob_range():
    candidates = generate_candidates(
        queue_models=["power_prob"],
        exponent_min=0.5, exponent_max=3.0, exponent_step=0.5,
    )
    exponents = [c.exponent for c in candidates if c.queue_model == "power_prob"]
    assert exponents == [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]


def test_generate_candidates_log_prob_no_exponent():
    candidates = generate_candidates(
        queue_models=["log_prob"],
        exponent_min=0.5, exponent_max=3.0, exponent_step=0.5,
    )
    assert len(candidates) == 1
    assert candidates[0].queue_model == "log_prob"
    assert candidates[0].exponent is None


def test_queue_model_candidate_label():
    assert QueueModelCandidate("power_prob", 1.5).label() == "power_prob(1.5)"
    assert QueueModelCandidate("log_prob", None).label() == "log_prob"


def test_sweep_exponent_picks_best_candidate():
    live_fills = {
        "2026-03-01": DailyFillSummary("2026-03-01", n_fills=10, adverse_pct=0.2, pnl=100.0),
    }

    def fake_run_replay(candidate, date):
        # Make exponent=1.5 produce perfect match, others produce worse match
        if candidate.exponent == 1.5:
            return DailyFillSummary(date, n_fills=10, adverse_pct=0.2, pnl=100.0)
        return DailyFillSummary(date, n_fills=3, adverse_pct=0.5, pnl=-50.0)

    candidates = generate_candidates(
        queue_models=["power_prob"],
        exponent_min=1.0, exponent_max=2.0, exponent_step=0.5,
    )
    result = sweep_exponent(
        instrument="TMFD6",
        candidates=candidates,
        calibration_days=["2026-03-01"],
        live_fills=live_fills,
        run_replay=fake_run_replay,
    )
    assert result.best_candidate.exponent == 1.5
    assert result.best_score.composite() > 0.9


def test_sweep_exponent_tie_break_by_insertion_order():
    """When multiple candidates tie on composite score, first-inserted wins."""
    live_fills = {
        "2026-03-01": DailyFillSummary("2026-03-01", n_fills=10, adverse_pct=0.2, pnl=100.0),
    }

    def fake_run_replay(candidate, date):
        # Every candidate produces the same match -> tie
        return DailyFillSummary(date, n_fills=10, adverse_pct=0.2, pnl=100.0)

    candidates = generate_candidates(
        queue_models=["power_prob"],
        exponent_min=1.0, exponent_max=2.0, exponent_step=0.5,
    )
    result = sweep_exponent(
        instrument="TMFD6", candidates=candidates,
        calibration_days=["2026-03-01"], live_fills=live_fills,
        run_replay=fake_run_replay,
    )
    # All candidates tie; `max` keeps first occurrence
    assert result.best_candidate == candidates[0]


def test_sweep_exponent_raises_on_empty_candidates():
    live_fills = {
        "2026-03-01": DailyFillSummary("2026-03-01", n_fills=10, adverse_pct=0.2, pnl=100.0),
    }

    def fake_run_replay(candidate, date):
        return DailyFillSummary(date, n_fills=10, adverse_pct=0.2, pnl=100.0)

    with pytest.raises(ValueError, match="candidates list is empty"):
        sweep_exponent(
            instrument="TMFD6", candidates=[],
            calibration_days=["2026-03-01"], live_fills=live_fills,
            run_replay=fake_run_replay,
        )


def test_generate_candidates_rejects_non_positive_step():
    with pytest.raises(ValueError, match="exponent_step must be positive"):
        generate_candidates(
            queue_models=["power_prob"],
            exponent_min=0.5, exponent_max=3.0, exponent_step=0,
        )


def test_generate_candidates_no_duplicates_with_fine_step():
    candidates = generate_candidates(
        queue_models=["power_prob"],
        exponent_min=1.0, exponent_max=1.01, exponent_step=0.001,
    )
    exponents = [c.exponent for c in candidates]
    assert len(exponents) == len(set(exponents))
    assert len(exponents) == 11  # 1.000, 1.001, ..., 1.010
