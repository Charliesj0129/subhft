"""Tests: Prometheus metrics emitted by hft_platform.alpha.audit.

Verifies:
- log_gate_result increments alpha_gate_results_total
- log_promotion_result increments alpha_promotion_results_total
- log_canary_action increments alpha_canary_actions_total
- Metrics fire even when HFT_ALPHA_AUDIT_ENABLED=0
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gate_report(gate: str = "Gate A", passed: bool = True):
    from hft_platform.alpha.validation import GateReport

    return GateReport(gate=gate, passed=passed, details={"info": "test"})


def _make_promotion_result(approved: bool = True, forced: bool = False):
    from hft_platform.alpha.promotion import PromotionResult

    return PromotionResult(
        alpha_id="alpha_x",
        approved=approved,
        forced=forced,
        gate_d_passed=approved,
        gate_e_passed=approved,
        gate_f_passed=approved,
        canary_weight=0.05,
        integration_report_path="/tmp/integration.json",
        promotion_decision_path="/tmp/decision.json",
        promotion_config_path="/tmp/promo.yaml",
        reasons=["test"],
    )


def _mock_metrics():
    """Return a MagicMock that mimics the MetricsRegistry counter interface."""
    m = MagicMock()
    # Make .labels(...).inc() chainable on the counter mocks
    m.alpha_gate_results_total.labels.return_value = MagicMock()
    m.alpha_promotion_results_total.labels.return_value = MagicMock()
    m.alpha_canary_actions_total.labels.return_value = MagicMock()
    return m


# ---------------------------------------------------------------------------
# log_gate_result
# ---------------------------------------------------------------------------


class TestGateResultMetrics:
    def test_pass_increments_counter(self):
        """log_gate_result with a passing report calls inc() with result='pass'."""
        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            import hft_platform.alpha.audit as audit

            audit.log_gate_result("alpha_x", "run_1", _make_gate_report(passed=True), None)

        mock_m.alpha_gate_results_total.labels.assert_called_once_with(
            alpha_id="alpha_x",
            gate="A",
            result="pass",
        )
        mock_m.alpha_gate_results_total.labels.return_value.inc.assert_called_once()

    def test_fail_increments_counter_with_fail_label(self):
        """log_gate_result with a failing report calls inc() with result='fail'."""
        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            import hft_platform.alpha.audit as audit

            audit.log_gate_result("alpha_x", None, _make_gate_report(passed=False), None)

        mock_m.alpha_gate_results_total.labels.assert_called_once_with(
            alpha_id="alpha_x",
            gate="A",
            result="fail",
        )
        mock_m.alpha_gate_results_total.labels.return_value.inc.assert_called_once()

    def test_gate_letter_stripped_correctly(self):
        """Gate label should be just the letter, e.g. 'B' not 'Gate B'."""
        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            import hft_platform.alpha.audit as audit

            audit.log_gate_result("alpha_x", None, _make_gate_report(gate="Gate B"), None)

        _, kwargs = mock_m.alpha_gate_results_total.labels.call_args
        assert kwargs["gate"] == "B"

    def test_metrics_fire_when_audit_disabled(self, monkeypatch):
        """Counter increments regardless of HFT_ALPHA_AUDIT_ENABLED value."""
        monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
        import hft_platform.alpha.audit as audit

        audit._ENABLED = None  # reset cached flag

        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            audit.log_gate_result("alpha_x", None, _make_gate_report(), None)

        mock_m.alpha_gate_results_total.labels.return_value.inc.assert_called_once()

    def test_none_metrics_does_not_raise(self):
        """When get_metrics() returns None, log_gate_result must not raise."""
        with patch("hft_platform.observability.metrics.get_metrics", return_value=None):
            import hft_platform.alpha.audit as audit

            # Should complete without raising
            result = audit.log_gate_result("alpha_x", None, _make_gate_report(), None)

        # Metrics path is skipped gracefully — function returns None
        assert result is None

    def test_metrics_exception_is_swallowed(self):
        """If the counter raises unexpectedly, log_gate_result must not propagate."""
        broken_m = MagicMock()
        broken_m.alpha_gate_results_total.labels.side_effect = RuntimeError("boom")
        with patch("hft_platform.observability.metrics.get_metrics", return_value=broken_m):
            import hft_platform.alpha.audit as audit

            # Must not raise
            result = audit.log_gate_result("alpha_x", None, _make_gate_report(), None)

        # Metrics exception is swallowed — function returns None
        assert result is None


# ---------------------------------------------------------------------------
# log_promotion_result
# ---------------------------------------------------------------------------


class TestPromotionResultMetrics:
    def test_approved_increments_counter(self):
        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            import hft_platform.alpha.audit as audit

            audit.log_promotion_result(_make_promotion_result(approved=True, forced=False), None)

        mock_m.alpha_promotion_results_total.labels.assert_called_once_with(
            alpha_id="alpha_x",
            result="approved",
        )
        mock_m.alpha_promotion_results_total.labels.return_value.inc.assert_called_once()

    def test_rejected_increments_counter(self):
        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            import hft_platform.alpha.audit as audit

            audit.log_promotion_result(_make_promotion_result(approved=False, forced=False), None)

        _, kwargs = mock_m.alpha_promotion_results_total.labels.call_args
        assert kwargs["result"] == "rejected"

    def test_forced_takes_priority_over_approved(self):
        """When forced=True, result label should be 'forced'."""
        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            import hft_platform.alpha.audit as audit

            audit.log_promotion_result(_make_promotion_result(approved=True, forced=True), None)

        _, kwargs = mock_m.alpha_promotion_results_total.labels.call_args
        assert kwargs["result"] == "forced"

    def test_metrics_fire_when_audit_disabled(self, monkeypatch):
        monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
        import hft_platform.alpha.audit as audit

        audit._ENABLED = None

        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            audit.log_promotion_result(_make_promotion_result(), None)

        mock_m.alpha_promotion_results_total.labels.return_value.inc.assert_called_once()


# ---------------------------------------------------------------------------
# log_canary_action
# ---------------------------------------------------------------------------


class TestCanaryActionMetrics:
    def test_action_label_passed_through(self):
        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            import hft_platform.alpha.audit as audit

            audit.log_canary_action("alpha_x", "graduated", 0.1, 1.0, "passed all checks")

        mock_m.alpha_canary_actions_total.labels.assert_called_once_with(
            alpha_id="alpha_x",
            action="graduated",
        )
        mock_m.alpha_canary_actions_total.labels.return_value.inc.assert_called_once()

    def test_rollback_action(self):
        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            import hft_platform.alpha.audit as audit

            audit.log_canary_action("alpha_x", "rolled_back", 0.05, 0.0, "drawdown breach")

        _, kwargs = mock_m.alpha_canary_actions_total.labels.call_args
        assert kwargs["action"] == "rolled_back"

    def test_metrics_fire_when_audit_disabled(self, monkeypatch):
        monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
        import hft_platform.alpha.audit as audit

        audit._ENABLED = None

        mock_m = _mock_metrics()
        with patch("hft_platform.observability.metrics.get_metrics", return_value=mock_m):
            audit.log_canary_action("alpha_x", "hold", 0.05, 0.05, "within bounds")

        mock_m.alpha_canary_actions_total.labels.return_value.inc.assert_called_once()


# ---------------------------------------------------------------------------
# get_metrics() module-level helper
# ---------------------------------------------------------------------------


class TestGetMetricsHelper:
    def test_returns_none_when_not_initialised(self):
        """get_metrics() returns None when MetricsRegistry._instance is None."""
        from hft_platform.observability.metrics import MetricsRegistry, get_metrics

        original = MetricsRegistry._instance
        try:
            MetricsRegistry._instance = None
            result = get_metrics()
            assert result is None
        finally:
            MetricsRegistry._instance = original

    def test_returns_instance_when_present(self):
        """get_metrics() returns the existing singleton when already initialised."""
        from hft_platform.observability.metrics import MetricsRegistry, get_metrics

        original = MetricsRegistry._instance
        fake = MagicMock()
        try:
            MetricsRegistry._instance = fake
            result = get_metrics()
            assert result is fake
        finally:
            MetricsRegistry._instance = original
