import pytest

from research.calibration.scoring import (
    CalibrationScore,
    compute_fill_rate_score,
    compute_adverse_fill_score,
    compute_pnl_direction_score,
    compute_pnl_magnitude_score,
    compute_score,
    DailyFillSummary,
)


def test_fill_rate_score_perfect_match():
    assert compute_fill_rate_score(sim=10.0, live=10.0) == 1.0


def test_fill_rate_score_50pct_off():
    # sim=15 vs live=10 → 1 - 5/10 = 0.5
    assert compute_fill_rate_score(sim=15.0, live=10.0) == 0.5


def test_fill_rate_score_live_zero_returns_zero():
    assert compute_fill_rate_score(sim=5.0, live=0.0) == 0.0


def test_fill_rate_score_clips_at_zero():
    # sim=30 vs live=10 → 1 - 20/10 = -1 → clipped to 0
    assert compute_fill_rate_score(sim=30.0, live=10.0) == 0.0


def test_adverse_fill_score_perfect_match():
    assert compute_adverse_fill_score(sim_pct=0.2, live_pct=0.2) == 1.0


def test_adverse_fill_score_large_diff():
    # |0.4 - 0.2| / max(0.2, 1) = 0.2 → 1 - 0.2 = 0.8
    assert compute_adverse_fill_score(sim_pct=0.4, live_pct=0.2) == pytest.approx(0.8)


def test_pnl_direction_score_all_match():
    sim = [10.0, -5.0, 3.0]
    live = [20.0, -1.0, 0.5]
    assert compute_pnl_direction_score(sim, live) == 1.0


def test_pnl_direction_score_half_match():
    sim = [10.0, 5.0, -3.0, 2.0]
    live = [10.0, -5.0, -3.0, -2.0]
    assert compute_pnl_direction_score(sim, live) == 0.5


def test_pnl_direction_score_empty_returns_zero():
    assert compute_pnl_direction_score([], []) == 0.0


def test_pnl_magnitude_score_perfect():
    assert compute_pnl_magnitude_score(sim=100.0, live=100.0) == 1.0


def test_pnl_magnitude_score_10pct_off():
    # |110 - 100| / 100 = 0.1 → 1 - 0.1 = 0.9
    assert compute_pnl_magnitude_score(sim=110.0, live=100.0) == pytest.approx(0.9)


def test_pnl_magnitude_score_live_zero_returns_zero():
    assert compute_pnl_magnitude_score(sim=100.0, live=0.0) == 0.0


def test_compute_score_composite():
    sim_days = [DailyFillSummary(date="2026-03-01", n_fills=10, adverse_pct=0.2, pnl=100.0)]
    live_days = [DailyFillSummary(date="2026-03-01", n_fills=10, adverse_pct=0.2, pnl=100.0)]
    score = compute_score(sim_days, live_days)
    assert score.composite() == 1.0


def test_compute_score_default_weights_sum_to_one():
    score = CalibrationScore(
        fill_rate_score=1.0,
        adverse_fill_score=1.0,
        pnl_direction_score=1.0,
        pnl_magnitude_score=1.0,
    )
    assert score.composite() == pytest.approx(1.0)


def test_compute_score_weighted():
    score = CalibrationScore(
        fill_rate_score=1.0,
        adverse_fill_score=0.0,
        pnl_direction_score=0.0,
        pnl_magnitude_score=0.0,
    )
    # default weights: (0.35, 0.25, 0.25, 0.15)
    assert score.composite() == pytest.approx(0.35)
