"""Tests for IntentDiff and ReplayParityReport (Slice C task 6)."""

from __future__ import annotations

from hft_platform.alpha.replay_parity import IntentDiff, ReplayParityReport


def _make_canonical_intent(idx: int, price: int = 100) -> dict[str, object]:
    """Build a canonical-schema intent dict for testing."""
    return {
        "intent_id": idx,
        "symbol": "TXFD6",
        "side": "BUY",
        "price": price,
        "qty": 1,
        "ts_ns": 1_000_000 + idx,
    }


def test_identical_streams_match_100() -> None:
    # Arrange
    live = [_make_canonical_intent(i) for i in range(10)]
    replayed = [_make_canonical_intent(i) for i in range(10)]
    diff = IntentDiff(live=live, replayed=replayed, evidence_path="/tmp/evidence.json")

    # Act
    report = diff.compute()

    # Assert
    assert isinstance(report, ReplayParityReport)
    assert report.match_pct == 100.0
    assert report.first_divergence_idx is None
    assert report.n_compared == 10
    assert report.divergence_histogram == {}


def test_one_field_diverges_at_idx_5() -> None:
    # Arrange
    live = [_make_canonical_intent(i) for i in range(10)]
    replayed = [_make_canonical_intent(i) for i in range(10)]
    replayed[5]["price"] = 999  # mutate price at idx 5
    diff = IntentDiff(live=live, replayed=replayed)

    # Act
    report = diff.compute()

    # Assert
    assert report.match_pct == 90.0
    assert report.first_divergence_idx == 5
    assert report.n_compared == 10
    assert report.divergence_histogram.get("price", 0) >= 1


def test_length_mismatch_handled() -> None:
    # Arrange
    live = [_make_canonical_intent(i) for i in range(10)]
    replayed = [_make_canonical_intent(i) for i in range(8)]
    diff = IntentDiff(live=live, replayed=replayed)

    # Act
    report = diff.compute()

    # Assert
    assert report.divergence_histogram.get("__missing__", 0) >= 2
    assert report.n_compared == 10
