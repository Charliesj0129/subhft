"""Tests for autonomy_transitions_total reason label sanitization.

Verifies that unknown reason strings are capped to "unknown" to prevent
unbounded Prometheus label cardinality.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.ops.autonomy import (
    _ALLOWED_REASON_CODES,
    AutonomyMode,
    AutonomyTransition,
    _reason_code_for_metrics,
    _scope_code_for_metrics,
)


class TestReasonCodeForMetrics:
    def test_known_reason_returned_as_is(self) -> None:
        for reason in _ALLOWED_REASON_CODES - {"unknown"}:
            assert _reason_code_for_metrics(reason) == reason

    def test_unknown_reason_maps_to_unknown(self) -> None:
        assert _reason_code_for_metrics("some_arbitrary_free_form_string") == "unknown"

    def test_empty_string_maps_to_unknown(self) -> None:
        assert _reason_code_for_metrics("") == "unknown"

    def test_reason_with_special_chars_normalized_then_checked(self) -> None:
        # "pnl-soft-limit" normalizes to "pnl_soft_limit" which IS in the known set
        assert _reason_code_for_metrics("pnl-soft-limit") == "pnl_soft_limit"

    def test_reason_normalized_but_not_in_set_maps_to_unknown(self) -> None:
        # "random-reason" normalizes to "random_reason" which is NOT in the known set
        assert _reason_code_for_metrics("random-reason") == "unknown"

    def test_uppercase_reason_normalized_and_matched(self) -> None:
        # "MANUAL_OPERATOR" normalizes to "manual_operator" which IS in the known set
        assert _reason_code_for_metrics("MANUAL_OPERATOR") == "manual_operator"

    def test_unknown_literal_in_allowed_set(self) -> None:
        assert "unknown" in _ALLOWED_REASON_CODES

    @pytest.mark.parametrize(
        "reason",
        [
            "free form reason with spaces",
            "injected';DROP TABLE--",
            "a" * 200,
            "新原因",
        ],
    )
    def test_arbitrary_inputs_map_to_unknown(self, reason: str) -> None:
        assert _reason_code_for_metrics(reason) == "unknown"


class TestScopeCodeForMetrics:
    def test_known_scope_returned_as_is(self) -> None:
        assert _scope_code_for_metrics("platform") == "platform"
        assert _scope_code_for_metrics("strategy") == "strategy"

    def test_unknown_scope_maps_to_unknown(self) -> None:
        assert _scope_code_for_metrics("exotic_scope") == "unknown"


class TestAutonomyTransitionMetricLabels:
    def test_metric_labels_uses_sanitized_reason(self) -> None:
        transition = AutonomyTransition(
            scope="platform",
            from_mode=AutonomyMode.NORMAL,
            to_mode=AutonomyMode.HALT,
            reason="some_unknown_free_form_reason",
        )
        labels = transition.metric_labels()
        assert labels["reason"] == "unknown"

    def test_metric_labels_preserves_known_reason(self) -> None:
        transition = AutonomyTransition(
            scope="platform",
            from_mode=AutonomyMode.NORMAL,
            to_mode=AutonomyMode.HALT,
            reason="manual_operator",
        )
        labels = transition.metric_labels()
        assert labels["reason"] == "manual_operator"

    def test_metric_labels_sanitizes_scope(self) -> None:
        transition = AutonomyTransition(
            scope="unknown_scope",
            from_mode=AutonomyMode.NORMAL,
            to_mode=AutonomyMode.HALT,
            reason="manual_operator",
        )
        labels = transition.metric_labels()
        assert labels["scope"] == "unknown"

    def test_record_transition_calls_counter_with_sanitized_labels(self) -> None:
        mock_metrics = MagicMock()
        transition = AutonomyTransition(
            scope="platform",
            from_mode=AutonomyMode.NORMAL,
            to_mode=AutonomyMode.HALT,
            reason="free_form_unknown_reason",
        )
        transition.record_transition(mock_metrics)

        mock_metrics.autonomy_transitions_total.labels.assert_called_once_with(
            scope="platform",
            from_mode="NORMAL",
            to_mode="HALT",
            reason="unknown",
        )
        mock_metrics.autonomy_transitions_total.labels.return_value.inc.assert_called_once()

    def test_record_transition_with_known_reason_passes_through(self) -> None:
        mock_metrics = MagicMock()
        transition = AutonomyTransition(
            scope="strategy",
            from_mode=AutonomyMode.NORMAL,
            to_mode=AutonomyMode.STRATEGY_QUARANTINED,
            reason="strategy_exception",
        )
        transition.record_transition(mock_metrics)

        mock_metrics.autonomy_transitions_total.labels.assert_called_once_with(
            scope="strategy",
            from_mode="NORMAL",
            to_mode="STRATEGY_QUARANTINED",
            reason="strategy_exception",
        )
