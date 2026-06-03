"""Tests for IntentDiff and ReplayParityReport (Slice C task 6)."""

from __future__ import annotations

from pathlib import Path

from hft_platform.alpha.replay_parity import IntentDiff, ReplayParityReport
from hft_platform.replay.intent_log import ReplayedIntentLog

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "replay_parity"


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


def test_replay_only_extra_field_counts_as_divergence() -> None:
    # A field the replay emits but live lacks is an asymmetric-schema
    # divergence (implementation drift). Iterating only live's keys would
    # hide it and falsely certify parity at 100%.
    live = [_make_canonical_intent(0)]
    replayed = [{**_make_canonical_intent(0), "price_type": "LIMIT"}]
    diff = IntentDiff(live=live, replayed=replayed)

    report = diff.compute()

    assert report.match_pct == 0.0
    assert report.first_divergence_idx == 0
    assert report.divergence_histogram.get("price_type", 0) == 1


def test_live_only_extra_field_counts_as_divergence() -> None:
    # Symmetric case: a field present only on the live side must also count.
    live = [{**_make_canonical_intent(0), "target_order_id": 7}]
    replayed = [_make_canonical_intent(0)]
    diff = IntentDiff(live=live, replayed=replayed)

    report = diff.compute()

    assert report.match_pct == 0.0
    assert report.divergence_histogram.get("target_order_id", 0) == 1


def test_present_none_differs_from_absent_field() -> None:
    # A field present with value None on one side and absent on the other is
    # a real divergence: the sentinel must not collapse absent == None.
    live = [{**_make_canonical_intent(0), "tif": None}]
    replayed = [_make_canonical_intent(0)]
    diff = IntentDiff(live=live, replayed=replayed)

    report = diff.compute()

    assert report.match_pct == 0.0
    assert report.divergence_histogram.get("tif", 0) == 1


def test_observed_fields_reports_union_of_compared_keys() -> None:
    # observed_fields must list every key seen across both streams so the gate
    # can tell which §7 dimensions were actually checked vs silently absent.
    live = [{**_make_canonical_intent(0), "session_phase": "RTH"}]
    replayed = [{**_make_canonical_intent(0), "force_flat_triggered": False}]
    diff = IntentDiff(live=live, replayed=replayed)

    report = diff.compute()

    assert "session_phase" in report.observed_fields
    assert "force_flat_triggered" in report.observed_fields
    assert "qty" in report.observed_fields
    # A dimension present on neither side stays absent — not assumed covered.
    assert "risk_filter_active" not in report.observed_fields


def test_empty_streams_report_no_observed_fields() -> None:
    report = IntentDiff(live=[], replayed=[]).compute()

    assert report.observed_fields == ()


def test_r47_oe1_synthetic_fixture_below_threshold() -> None:
    # Arrange — load hand-constructed live + replayed canonical streams.
    # Live omits 6 CANCEL intents (OE1 cancel-path short-circuit, 2026-04-21).
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "synthetic_r47_oe1_live.jsonl")
    replayed = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "synthetic_r47_oe1_replayed.jsonl")

    # Act
    report = IntentDiff(
        live=live.canonical_records(),
        replayed=replayed.canonical_records(),
        evidence_path="synthetic_r47_oe1",
    ).compute()

    # Assert — DoD-C1 path-(b): parity gate must catch this divergence.
    assert report.match_pct < 95.0
    assert report.divergence_histogram.get("__missing__", 0) >= 6
    assert report.n_compared == 100


def test_clean_echo_fixture_at_100() -> None:
    # Arrange — DoD-C2: gate must NOT always-fail on a clean echo.
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "clean_echo_live.jsonl")
    replayed = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "clean_echo_replayed.jsonl")

    # Act
    report = IntentDiff(
        live=live.canonical_records(),
        replayed=replayed.canonical_records(),
        evidence_path="clean_echo",
    ).compute()

    # Assert
    assert report.match_pct == 100.0
    assert report.first_divergence_idx is None
    assert report.divergence_histogram == {}
    assert report.n_compared == 100
