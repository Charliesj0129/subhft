import pytest

from research.calibration.scoring import CalibrationScore, DailyFillSummary
from research.calibration.sweep import QueueModelCandidate, SweepResult
from research.calibration.validate import (
    determine_confidence,
    split_days,
    validate_on_heldout,
)


def test_split_days_raises_on_empty():
    with pytest.raises(ValueError, match="at least 1 day"):
        split_days([])


def test_split_days_sufficient_uses_70_30():
    days = [f"2026-03-{i:02d}" for i in range(1, 16)]  # 15 days
    train, test = split_days(days, ratio=0.7)
    assert len(train) == 10
    assert len(test) == 5
    assert set(train) | set(test) == set(days)


def test_split_days_low_count_uses_loo():
    days = [f"2026-03-{i:02d}" for i in range(1, 8)]  # 7 days
    train, test = split_days(days, ratio=0.7)
    # < 10 days: leave-one-out means test has 1, train has rest
    assert len(test) == 1
    assert len(train) == 6


def test_determine_confidence():
    assert determine_confidence(days=20, score=0.8) == "high"
    assert determine_confidence(days=10, score=0.75) == "medium"
    assert determine_confidence(days=6, score=0.65) == "low"
    assert determine_confidence(days=3, score=0.9) == "low"


def test_validate_on_heldout_uses_best_candidate():
    best = QueueModelCandidate("power_prob", 1.5)
    sweep_result = SweepResult(
        instrument="TMFD6",
        best_candidate=best,
        best_score=CalibrationScore(0.8, 0.8, 0.8, 0.8),
    )
    live_fills = {
        "2026-03-10": DailyFillSummary("2026-03-10", n_fills=10, adverse_pct=0.2, pnl=100.0),
    }

    def fake_replay(candidate, date):
        assert candidate == best
        return DailyFillSummary(date, n_fills=10, adverse_pct=0.2, pnl=100.0)

    result = validate_on_heldout(
        sweep_result=sweep_result,
        heldout_days=["2026-03-10"],
        live_fills=live_fills,
        run_replay=fake_replay,
    )
    assert result.composite() == pytest.approx(1.0)
