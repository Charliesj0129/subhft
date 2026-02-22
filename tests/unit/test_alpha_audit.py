"""Tests for hft_platform.alpha.audit module."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


def _make_gate_report(gate: str = "Gate A", passed: bool = True, details: dict | None = None):
    from hft_platform.alpha.validation import GateReport

    return GateReport(gate=gate, passed=passed, details=details or {"info": "test"})


def _make_promotion_result(approved: bool = True, forced: bool = False):
    from hft_platform.alpha.promotion import PromotionResult

    return PromotionResult(
        alpha_id="test_alpha",
        approved=approved,
        forced=forced,
        gate_d_passed=approved,
        gate_e_passed=approved,
        canary_weight=0.05,
        integration_report_path="/tmp/integration.json",
        promotion_decision_path="/tmp/decision.json",
        promotion_config_path="/tmp/promo.yaml",
        reasons=["test reason"],
    )


class TestAuditDisabledByDefault:
    """When HFT_ALPHA_AUDIT_ENABLED is not set (default), all audit calls are no-ops."""

    def setup_method(self):
        import hft_platform.alpha.audit as mod

        mod._ENABLED = None

    def test_log_gate_result_noop_when_disabled(self):
        from hft_platform.alpha.audit import log_gate_result

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "0"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            # Should not raise, should not call clickhouse
            log_gate_result("alpha1", "run1", _make_gate_report(), "hash123")

    def test_log_promotion_result_noop_when_disabled(self):
        from hft_platform.alpha.audit import log_promotion_result

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "0"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            log_promotion_result(_make_promotion_result(), "hash123", {"sharpe_oos": 1.5})

    def test_log_canary_action_noop_when_disabled(self):
        from hft_platform.alpha.audit import log_canary_action

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "0"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            log_canary_action("alpha1", "rollback", 0.05, 0.0, "test", {})


class TestAuditEnabled:
    """When HFT_ALPHA_AUDIT_ENABLED=1, calls should attempt to insert into ClickHouse."""

    def setup_method(self):
        import hft_platform.alpha.audit as mod

        mod._ENABLED = None

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_gate_result_inserts_row(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        from hft_platform.alpha.audit import log_gate_result

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            report = _make_gate_report(gate="Gate B", passed=False, details={"err": "bad"})
            log_gate_result("alpha1", "run42", report, "cfg_hash")

        mock_client.insert.assert_called_once()
        call_args = mock_client.insert.call_args
        assert call_args[0][0] == "audit.alpha_gate_log"
        row = call_args[0][1][0]
        assert row[1] == "alpha1"  # alpha_id
        assert row[3] == "B"  # gate letter
        assert row[4] == 0  # passed = False

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_promotion_result_inserts_row(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        from hft_platform.alpha.audit import log_promotion_result

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            result = _make_promotion_result(approved=True, forced=False)
            log_promotion_result(result, "cfg_hash", {"sharpe_oos": 1.8})

        mock_client.insert.assert_called_once()
        call_args = mock_client.insert.call_args
        assert call_args[0][0] == "audit.alpha_promotion_log"
        row = call_args[0][1][0]
        assert row[1] == "test_alpha"  # alpha_id
        assert row[3] == 1  # approved

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_canary_action_inserts_row(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        from hft_platform.alpha.audit import log_canary_action

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            log_canary_action("alpha1", "rollback", 0.05, 0.0, "slippage exceeded", {"slippage": 5.0})

        mock_client.insert.assert_called_once()
        call_args = mock_client.insert.call_args
        assert call_args[0][0] == "audit.alpha_canary_log"
        row = call_args[0][1][0]
        assert row[1] == "alpha1"
        assert row[2] == "rollback"
        assert row[3] == 0.05  # old_weight
        assert row[4] == 0.0  # new_weight

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_gate_result_fails_silently(self, mock_get_client):
        mock_get_client.side_effect = ConnectionError("no CH")

        from hft_platform.alpha.audit import log_gate_result

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            # Should not raise
            log_gate_result("alpha1", "run1", _make_gate_report(), "hash")

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_promotion_result_fails_silently(self, mock_get_client):
        mock_get_client.side_effect = ConnectionError("no CH")

        from hft_platform.alpha.audit import log_promotion_result

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            # Should not raise
            log_promotion_result(_make_promotion_result(), "hash")
