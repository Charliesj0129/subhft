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
        assert classify_field("timestamp_us") is DivergenceCategory.TIMESTAMP_ALIGNMENT_ERROR

    def test_decision_price_maps_to_latency_shift(self) -> None:
        assert classify_field("decision_price") is DivergenceCategory.LATENCY_SHIFT

    def test_qty_maps_to_position_limit(self) -> None:
        assert classify_field("qty") is DivergenceCategory.POSITION_LIMIT

    def test_strategy_id_maps_to_implementation_drift(self) -> None:
        assert classify_field("strategy_id") is DivergenceCategory.IMPLEMENTATION_DRIFT

    def test_unknown_key_maps_to_unknown(self) -> None:
        assert classify_field("not_a_real_field") is DivergenceCategory.UNKNOWN

    # --- Round 14: prefix matching for the three previously-unmapped
    # divergence categories.  Any sidecar field starting with these
    # prefixes routes to the right triage owner without waiting on a
    # canonical-intent schema rev.

    def test_feature_prefix_maps_to_feature_mismatch(self) -> None:
        assert classify_field("feature_obi") is DivergenceCategory.FEATURE_MISMATCH
        assert classify_field("feature_microprice") is DivergenceCategory.FEATURE_MISMATCH

    def test_session_prefix_maps_to_session_phase_filter(self) -> None:
        assert classify_field("session_phase") is DivergenceCategory.SESSION_PHASE_FILTER
        assert classify_field("session_window_active") is DivergenceCategory.SESSION_PHASE_FILTER

    def test_risk_prefix_maps_to_risk_filter(self) -> None:
        assert classify_field("risk_filter_breach") is DivergenceCategory.RISK_FILTER
        assert classify_field("risk_brake_active") is DivergenceCategory.RISK_FILTER

    def test_exact_field_match_wins_over_prefix(self) -> None:
        # Defensive: an exact-mapped field must NOT be re-routed by a
        # prefix rule.  Today no canonical field starts with these
        # prefixes but the contract should be explicit.
        assert classify_field("symbol") is DivergenceCategory.DATA_MISMATCH  # control — no prefix conflict
        # Force a synthetic check: every exact mapping should still
        # round-trip regardless of prefix table.
        for canonical_key in (
            "__missing__",
            "symbol",
            "timestamp_us",
            "decision_price",
            "qty",
            "strategy_id",
        ):
            cat = classify_field(canonical_key)
            assert cat is not DivergenceCategory.UNKNOWN
            # Specifically: prefix-matched categories should not steal
            # an exact-mapped field.
            assert cat not in {
                DivergenceCategory.FEATURE_MISMATCH,
                DivergenceCategory.SESSION_PHASE_FILTER,
                DivergenceCategory.RISK_FILTER,
            }

    def test_prefix_without_suffix_still_routes(self) -> None:
        # Edge case: "feature_" alone (empty body) still matches the
        # prefix and routes to FEATURE_MISMATCH — operator producing
        # a malformed key still sees the divergence rather than UNKNOWN.
        assert classify_field("feature_") is DivergenceCategory.FEATURE_MISMATCH

    def test_unrelated_prefix_falls_through_to_unknown(self) -> None:
        # Sanity: a key that looks like a prefix neighbor but isn't
        # should still land in UNKNOWN.
        assert classify_field("featureless_x") is DivergenceCategory.UNKNOWN
        assert classify_field("sessionless") is DivergenceCategory.UNKNOWN

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
            # Round 15 optional parity fields (goal §7).  Emitted only
            # when the source intent carries a non-None value — but if
            # they ever do appear in a histogram, they must classify
            # rather than slip into UNKNOWN.
            "session_phase",
            "risk_filter_active",
            "force_flat_triggered",
        }
        for field in known:
            assert classify_field(field) is not DivergenceCategory.UNKNOWN, (
                f"{field!r} fell into UNKNOWN — update mapping"
            )

    def test_round15_parity_fields_route_to_correct_category(self) -> None:
        # Direct exact-match assertion (Round 15 adds these to
        # _FIELD_TO_CATEGORY).  The Round-14 prefix table would also
        # route them, but the contract is explicit at exact-match
        # status, so guard against accidental demotion.
        assert classify_field("session_phase") is DivergenceCategory.SESSION_PHASE_FILTER
        assert classify_field("risk_filter_active") is DivergenceCategory.RISK_FILTER
        assert classify_field("force_flat_triggered") is DivergenceCategory.RISK_FILTER


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
            "feature_obi": 3,  # Round 14 prefix -> feature_mismatch
            "session_phase": 2,  # Round 14 prefix -> session_phase_filter
            "risk_filter_breach": 4,  # Round 14 prefix -> risk_filter
            "unmapped_field": 1,
        }
        out = categorize_histogram(hist)
        assert out[DivergenceCategory.DATA_MISMATCH.value] == 5  # 3 + 2
        assert out[DivergenceCategory.TIMESTAMP_ALIGNMENT_ERROR.value] == 5
        assert out[DivergenceCategory.LATENCY_SHIFT.value] == 1
        assert out[DivergenceCategory.POSITION_LIMIT.value] == 4
        assert out[DivergenceCategory.IMPLEMENTATION_DRIFT.value] == 9  # 7 + 2
        assert out[DivergenceCategory.FEATURE_MISMATCH.value] == 3
        assert out[DivergenceCategory.SESSION_PHASE_FILTER.value] == 2
        assert out[DivergenceCategory.RISK_FILTER.value] == 4
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
