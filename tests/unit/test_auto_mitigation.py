"""Unit tests for alert auto-mitigation framework (Unit 5)."""

from __future__ import annotations

from hft_platform.incident.actions import (
    CancelOpenOrdersAction,
    LogAndEscalateAction,
    MitigationAction,
    NoOpAction,
    ReconnectBrokerAction,
    RestartServiceAction,
    SwitchRecorderModeAction,
)
from hft_platform.incident.auto_mitigation import AlertMitigator


class TestAlertMitigator:
    def test_feed_gap_critical(self):
        mitigator = AlertMitigator()
        action = mitigator.evaluate("FeedGapCritical")
        assert action.action_type == "reconnect_broker"
        assert action.safe is True

    def test_recorder_failure(self):
        mitigator = AlertMitigator()
        action = mitigator.evaluate("RecorderFailure")
        assert action.action_type == "switch_recorder_mode"
        assert action.params["target_mode"] == "wal_first"

    def test_bus_overflow_critical(self):
        mitigator = AlertMitigator()
        action = mitigator.evaluate("BusOverflowCritical")
        assert action.action_type == "log_and_escalate"

    def test_storm_guard_halt(self):
        mitigator = AlertMitigator()
        action = mitigator.evaluate("StormGuardHalt")
        assert action.action_type == "cancel_open_orders"
        assert action.safe is True

    def test_execution_gateway_down(self):
        mitigator = AlertMitigator()
        action = mitigator.evaluate("ExecutionGatewayTaskDown")
        assert action.action_type == "restart_service"
        assert action.params["service"] == "exec_gateway"

    def test_unknown_alert_returns_noop(self):
        mitigator = AlertMitigator()
        action = mitigator.evaluate("SomeUnknownAlert")
        assert action.action_type == "noop"
        assert action.safe is True

    def test_labels_passed_through(self):
        mitigator = AlertMitigator()
        labels = {"instance": "hft-engine:9090", "severity": "critical"}
        action = mitigator.evaluate("FeedGapCritical", labels=labels)
        assert action.params["labels"] == labels

    def test_execute_returns_action_and_result(self):
        mitigator = AlertMitigator()
        mitigation, result = mitigator.execute("FeedGapCritical")
        assert mitigation.action_type == "reconnect_broker"
        assert result is not None
        assert result.success is True

    def test_execute_noop_for_unknown(self):
        mitigator = AlertMitigator()
        mitigation, result = mitigator.execute("UnknownAlert")
        assert mitigation.action_type == "noop"
        assert result.success is True


class TestActions:
    def test_reconnect_broker(self):
        action = ReconnectBrokerAction(max_retries=5, backoff_base_s=1.0)
        assert action.is_safe()
        result = action.execute()
        assert result.success
        assert "max_retries=5" in result.message

    def test_switch_recorder_mode(self):
        action = SwitchRecorderModeAction(target_mode="wal_first")
        assert action.is_safe()
        result = action.execute()
        assert result.success
        assert "wal_first" in result.message

    def test_cancel_open_orders(self):
        action = CancelOpenOrdersAction()
        assert action.is_safe()
        result = action.execute()
        assert result.success

    def test_log_and_escalate(self):
        action = LogAndEscalateAction(severity="warning")
        assert action.is_safe()
        result = action.execute()
        assert "warning" in result.message

    def test_restart_service(self):
        action = RestartServiceAction(service="exec_gateway")
        result = action.execute()
        assert "exec_gateway" in result.message

    def test_noop_action(self):
        action = NoOpAction(reason="test reason")
        result = action.execute()
        assert result.success
        assert "test reason" in result.message


class TestMitigationAction:
    def test_to_dict(self):
        ma = MitigationAction(
            action_type="reconnect_broker",
            params={"max_retries": 3},
            safe=True,
            reason="auto",
            timestamp_ns=12345,
        )
        d = ma.to_dict()
        assert d["action_type"] == "reconnect_broker"
        assert d["safe"] is True
        assert d["timestamp_ns"] == 12345
