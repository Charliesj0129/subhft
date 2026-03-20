"""Unit tests for alert webhook receiver (Unit 6)."""

from __future__ import annotations


class TestProcessAlerts:
    def setup_method(self):
        # Import here to avoid early import issues
        import os
        import sys

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
        from alert_handler_webhook import _process_alerts

        self._process_alerts = _process_alerts

    def test_single_firing_alert(self):
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "FeedGapCritical", "severity": "critical"},
                    "annotations": {"summary": "Feed gap detected"},
                }
            ]
        }
        results = self._process_alerts(payload)
        assert len(results) == 1
        assert results[0]["alert_name"] == "FeedGapCritical"
        assert results[0]["action_type"] == "reconnect_broker"
        assert results[0]["success"] is True

    def test_resolved_alert_skipped(self):
        payload = {
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {"alertname": "FeedGapCritical"},
                    "annotations": {},
                }
            ]
        }
        results = self._process_alerts(payload)
        assert len(results) == 1
        assert results[0]["status"] == "skipped_resolved"

    def test_unknown_alert(self):
        payload = [
            {
                "status": "firing",
                "labels": {"alertname": "NewUnknownAlert"},
                "annotations": {},
            }
        ]
        results = self._process_alerts(payload)
        assert len(results) == 1
        assert results[0]["action_type"] == "noop"

    def test_multiple_alerts(self):
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "FeedGapCritical"},
                    "annotations": {},
                },
                {
                    "status": "firing",
                    "labels": {"alertname": "RecorderFailure"},
                    "annotations": {},
                },
            ]
        }
        results = self._process_alerts(payload)
        assert len(results) == 2
        assert results[0]["action_type"] == "reconnect_broker"
        assert results[1]["action_type"] == "switch_recorder_mode"

    def test_storm_guard_halt_dispatches_cancel(self):
        payload = [
            {
                "status": "firing",
                "labels": {"alertname": "StormGuardHalt"},
                "annotations": {},
            }
        ]
        results = self._process_alerts(payload)
        assert results[0]["action_type"] == "cancel_open_orders"
        assert results[0]["safe"] is True
