"""The drift-burst breaker's input had no metric, only an INFO log.

On 2026-08-10 StormGuard took three WARM excursions (toxicity 0.501/0.502/
0.508, ~39 s each) and recovered from all of them. Reading that off the
existing metrics was impossible: `stormguard_transitions_total` is labelled
only escalation/de_escalation, so three WARMs and three HALTs are the same
number, and the toxicity score driving them was not exported at all.

That matters because toxicity is a sigmoid of the drift t-statistic,
`2/(1+exp(-|T|/threshold))-1`, which is exactly 0.5 when |T| equals the burst
threshold. "WARM at 0.501" is therefore the *definition* of a marginal
crossing, not evidence of a badly placed threshold — but without the series
nobody can tell those two apart, which is why the hysteresis band added in P4
(2026-04-28) had to be diagnosed from logs.

These tests pin the metrics that make the breaker legible.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from prometheus_client import Counter, Gauge

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.storm_guard import StormGuardState


@pytest.mark.unit
def test_drift_burst_metrics_exist_with_expected_types_and_labels() -> None:
    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()

    assert isinstance(registry.stormguard_toxicity_score, Gauge)
    assert registry.stormguard_toxicity_score._labelnames == ("symbol",)

    assert isinstance(registry.stormguard_escalations_total, Counter)
    assert registry.stormguard_escalations_total._labelnames == ("to_state",)

    assert isinstance(registry.drift_burst_detected_total, Counter)
    assert registry.drift_burst_detected_total._labelnames == ("symbol", "toxicity_type")


def _guard_with_mock_metrics(metrics):
    from hft_platform.risk.storm_guard import StormGuard

    guard = StormGuard.__new__(StormGuard)
    guard.metrics = metrics
    return guard


@pytest.mark.unit
def test_escalation_counter_distinguishes_warm_from_halt() -> None:
    """Three WARM excursions must not read the same as three HALTs."""
    metrics = MagicMock()
    guard = _guard_with_mock_metrics(metrics)
    guard._pending_transition_emit = [
        {"new_state_int": int(StormGuardState.WARM), "old_state_int": int(StormGuardState.NORMAL)},
        {"new_state_int": int(StormGuardState.HALT), "old_state_int": int(StormGuardState.WARM)},
    ]

    guard._emit_pending_transition()

    to_states = [c.kwargs["to_state"] for c in metrics.stormguard_escalations_total.labels.call_args_list]
    assert to_states == ["WARM", "HALT"]


@pytest.mark.unit
def test_toxicity_score_is_published_on_every_evaluation() -> None:
    """The breaker's input, not just its output — including below the threshold.

    A score of 0.30 causes no state change at all, so without this gauge that
    evaluation leaves no trace and the distance to the 0.5 entry threshold is
    unobservable.
    """
    from hft_platform.risk.drift_burst_detector import ToxicityResult
    from hft_platform.risk.storm_guard import StormGuard

    metrics = MagicMock()
    metrics.cap_symbol.side_effect = lambda s: s
    guard = StormGuard.__new__(StormGuard)
    guard.metrics = metrics
    guard._drift_burst_detector = MagicMock()
    guard._drift_burst_detector.evaluate.return_value = ToxicityResult(
        burst_detected=False, toxicity_score=0.30, burst_event=None
    )
    guard.state = StormGuardState.NORMAL
    guard._warm_toxicity_entry = 0.5
    guard._warm_toxicity_exit = 0.4

    StormGuard.update_with_lob(guard, mid_price_x2=90000, spread_scaled=100, imbalance=0.1, ts=1, symbol="MXFH6")

    metrics.stormguard_toxicity_score.labels.assert_called_once_with(symbol="MXFH6")
    metrics.stormguard_toxicity_score.labels.return_value.set.assert_called_once_with(0.30)
    metrics.drift_burst_detected_total.labels.assert_not_called()


@pytest.mark.unit
def test_de_escalation_does_not_count_as_an_escalation() -> None:
    metrics = MagicMock()
    guard = _guard_with_mock_metrics(metrics)
    guard._pending_transition_emit = [
        {"new_state_int": int(StormGuardState.NORMAL), "old_state_int": int(StormGuardState.WARM)},
    ]

    guard._emit_pending_transition()

    metrics.stormguard_escalations_total.labels.assert_not_called()
