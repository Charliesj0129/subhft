"""Tests for the divergence-category classifier (goal §8)."""

from __future__ import annotations

from hft_platform.alpha.divergence_category import (
    DivergenceCategory,
    categorize_histogram,
    classify_field,
)


class TestClassifyField:
    def test_missing_maps_to_data_mismatch(self) -> None:
        assert classify_field("__missing__") is DivergenceCategory.DATA_MISMATCH

    def test_symbol_maps_to_data_mismatch(self) -> None:
        assert classify_field("symbol") is DivergenceCategory.DATA_MISMATCH

    def test_timestamp_us_maps_to_timestamp_alignment_error(self) -> None:
        assert (
            classify_field("timestamp_us")
            is DivergenceCategory.TIMESTAMP_ALIGNMENT_ERROR
        )

    def test_decision_price_maps_to_latency_shift(self) -> None:
        assert classify_field("decision_price") is DivergenceCategory.LATENCY_SHIFT

    def test_qty_maps_to_position_limit(self) -> None:
        assert classify_field("qty") is DivergenceCategory.POSITION_LIMIT

    def test_strategy_id_maps_to_implementation_drift(self) -> None:
        assert (
            classify_field("strategy_id") is DivergenceCategory.IMPLEMENTATION_DRIFT
        )

    def test_unknown_key_maps_to_unknown(self) -> None:
        assert classify_field("not_a_real_field") is DivergenceCategory.UNKNOWN

    def test_known_intent_fields_have_non_unknown_mapping(self) -> None:
        # Every key emitted by replay.intent_log._intent_to_canonical()
        # should classify to something other than UNKNOWN — leaving any
        # known key unmapped would silently mask divergences.
        known = {
            "intent_id",
            "strategy_id",
            "symbol",
            "intent_type",
            "side",
            "tif",
            "price",
            "qty",
            "target_order_id",
            "timestamp_us",
            "decision_price",
            "price_type",
        }
        for field in known:
            assert (
                classify_field(field) is not DivergenceCategory.UNKNOWN
            ), f"{field!r} fell into UNKNOWN — update mapping"


class TestCategorizeHistogram:
    def test_aggregates_counts_per_category(self) -> None:
        hist = {
            "__missing__": 3,
            "symbol": 2,
            "timestamp_us": 5,
            "decision_price": 1,
            "qty": 4,
            "side": 7,  # implementation_drift
            "intent_type": 2,  # implementation_drift
            "unmapped_field": 1,
        }
        out = categorize_histogram(hist)
        assert out[DivergenceCategory.DATA_MISMATCH.value] == 5  # 3 + 2
        assert out[DivergenceCategory.TIMESTAMP_ALIGNMENT_ERROR.value] == 5
        assert out[DivergenceCategory.LATENCY_SHIFT.value] == 1
        assert out[DivergenceCategory.POSITION_LIMIT.value] == 4
        assert out[DivergenceCategory.IMPLEMENTATION_DRIFT.value] == 9  # 7 + 2
        assert out[DivergenceCategory.UNKNOWN.value] == 1

    def test_empty_histogram_returns_empty_dict(self) -> None:
        assert categorize_histogram({}) == {}

    def test_only_returns_categories_with_nonzero_counts(self) -> None:
        out = categorize_histogram({"symbol": 1})
        assert out == {DivergenceCategory.DATA_MISMATCH.value: 1}

    def test_dominant_category_is_top_count(self) -> None:
        # caller uses this for "dominant_category" — make sure the
        # standard max() pattern reproduces what's expected.
        hist = {"side": 10, "timestamp_us": 2, "qty": 1}
        out = categorize_histogram(hist)
        dominant = max(out, key=out.get)
        assert dominant == DivergenceCategory.IMPLEMENTATION_DRIFT.value


class TestDivergenceCategoryEnum:
    def test_all_nine_categories_present(self) -> None:
        expected = {
            "data_mismatch",
            "feature_mismatch",
            "timestamp_alignment_error",
            "latency_shift",
            "session_phase_filter",
            "risk_filter",
            "position_limit",
            "implementation_drift",
            "unknown",
        }
        assert {c.value for c in DivergenceCategory} == expected
