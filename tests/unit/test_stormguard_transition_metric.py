"""Tests for StormGuard stormguard_transitions_total counter (T12)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.risk.storm_guard import StormGuard, StormGuardState


def _make_guard() -> tuple[StormGuard, MagicMock]:
    """Return a StormGuard and its mock metrics object."""
    mock_metrics = MagicMock()
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=mock_metrics):
        sg = StormGuard()
    # Disable cooldowns and set de-escalation threshold to 1 for fast recovery
    sg._storm_cooldown_s = 0.0
    sg._halt_cooldown_s = 0.0
    sg._de_escalate_threshold = 1
    return sg, mock_metrics


class TestStormGuardTransitionMetric:
    def test_escalation_increments_counter(self) -> None:
        """NORMAL → WARM triggers an escalation counter increment."""
        sg, mock_metrics = _make_guard()

        sg.update(drawdown_bps=-60)  # NORMAL → WARM

        mock_metrics.stormguard_transitions_total.labels.assert_called_with(direction="escalation")
        mock_metrics.stormguard_transitions_total.labels().inc.assert_called()

    def test_de_escalation_increments_counter(self) -> None:
        """HALT → NORMAL triggers a de_escalation counter increment."""
        sg, mock_metrics = _make_guard()

        sg.trigger_halt("test")  # NORMAL → HALT (escalation)

        # Reset call tracking so we only watch the recovery transition
        mock_metrics.reset_mock()

        sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)  # HALT → NORMAL

        calls = mock_metrics.stormguard_transitions_total.labels.call_args_list
        directions = [c.kwargs.get("direction") or (c.args[0] if c.args else None) for c in calls]
        assert "de_escalation" in directions

    def test_multiple_escalation_steps_counted(self) -> None:
        """Each escalation step (NORMAL→WARM, WARM→STORM, STORM→HALT) is counted."""
        sg, mock_metrics = _make_guard()

        sg.update(drawdown_bps=-60)   # NORMAL → WARM
        sg.update(drawdown_bps=-110)  # WARM → STORM
        sg.update(drawdown_bps=-210)  # STORM → HALT

        calls = mock_metrics.stormguard_transitions_total.labels.call_args_list
        directions = [c.kwargs.get("direction", c.args[0] if c.args else None) for c in calls]
        assert directions.count("escalation") >= 3

    def test_no_transition_no_counter_increment(self) -> None:
        """Staying in NORMAL state does not increment the counter."""
        sg, mock_metrics = _make_guard()

        sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)  # stays NORMAL

        mock_metrics.stormguard_transitions_total.labels.assert_not_called()

    def test_direction_label_value_for_escalation(self) -> None:
        """Verify direction label is exactly 'escalation' on escalation."""
        sg, mock_metrics = _make_guard()

        sg.update(drawdown_bps=-60)

        call_kwargs = mock_metrics.stormguard_transitions_total.labels.call_args
        assert call_kwargs is not None
        direction = call_kwargs.kwargs.get("direction") or (call_kwargs.args[0] if call_kwargs.args else None)
        assert direction == "escalation"

    def test_direction_label_value_for_de_escalation(self) -> None:
        """Verify direction label is exactly 'de_escalation' on de-escalation."""
        sg, mock_metrics = _make_guard()
        sg.trigger_halt("test")
        mock_metrics.reset_mock()

        sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)

        calls = mock_metrics.stormguard_transitions_total.labels.call_args_list
        directions = [c.kwargs.get("direction", c.args[0] if c.args else None) for c in calls]
        assert "de_escalation" in directions

    def test_counter_resilient_to_metrics_error(self) -> None:
        """If counter.inc() raises, transition still completes without propagating."""
        sg, mock_metrics = _make_guard()
        mock_metrics.stormguard_transitions_total.labels.return_value.inc.side_effect = RuntimeError("boom")

        # Should not raise; StormGuard state must change correctly
        new_state = sg.update(drawdown_bps=-60)
        assert new_state == StormGuardState.WARM
        assert sg.state == StormGuardState.WARM
