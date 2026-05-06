"""Tests for hft_platform.alpha.audit module."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


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
        gate_f_passed=approved,
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
            result = log_gate_result("alpha1", "run1", _make_gate_report(), "hash123")
            assert result is None

    def test_log_promotion_result_noop_when_disabled(self):
        from hft_platform.alpha.audit import log_promotion_result

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "0"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            result = log_promotion_result(_make_promotion_result(), "hash123", {"sharpe_oos": 1.5})
            assert result is None

    def test_log_canary_action_noop_when_disabled(self):
        from hft_platform.alpha.audit import log_canary_action

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "0"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            result = log_canary_action("alpha1", "rollback", 0.05, 0.0, "test", {})
            assert result is None


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
            # Should not raise — fails silently on connection error
            result = log_gate_result("alpha1", "run1", _make_gate_report(), "hash")
            assert result is None

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_promotion_result_fails_silently(self, mock_get_client):
        mock_get_client.side_effect = ConnectionError("no CH")

        from hft_platform.alpha.audit import log_promotion_result

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            # Should not raise — fails silently on connection error
            result = log_promotion_result(_make_promotion_result(), "hash")
            assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Slice-D Task 5: log_kill() + reason_class coarsening
# ─────────────────────────────────────────────────────────────────────────────


def _make_kill_record(**overrides):
    from hft_platform.alpha.kill_ledger import KillRecord

    base = dict(
        alpha_id="alpha1",
        gate="C",
        reason="failed gate C: invalid_data",
        stable_artifact_hash="hash_abc",
        scorecard_id="sc_001",
        killed_at=1_700_000_000_000_000_000,
    )
    base.update(overrides)
    return KillRecord(**base)


class TestClassifyKillReason:
    """The reason_class coarsening keeps Prometheus label cardinality bounded."""

    def test_inventory_mtm_bucket(self):
        from hft_platform.alpha.audit import _classify_kill_reason

        assert _classify_kill_reason("inventory_mtm: residual >0", "D") == "inventory_mtm"

    def test_cost_uncertainty_bucket(self):
        from hft_platform.alpha.audit import _classify_kill_reason

        assert _classify_kill_reason("cost_uncertainty band crosses zero", "D") == "cost_uncertainty"

    def test_screener_buckets(self):
        from hft_platform.alpha.audit import _classify_kill_reason

        assert _classify_kill_reason("ic_mean below threshold", "pre_screen") == "screener_ic"
        assert _classify_kill_reason("turnover too high", "pre_screen") == "screener_turnover"
        assert _classify_kill_reason("cost_floor breach", "pre_screen") == "screener_cost_floor"

    def test_replay_parity_bucket(self):
        from hft_platform.alpha.audit import _classify_kill_reason

        assert _classify_kill_reason("replay_parity divergence 7%", "D") == "replay_parity"

    def test_cluster_bucket(self):
        from hft_platform.alpha.audit import _classify_kill_reason

        assert _classify_kill_reason("redundant in cluster_3", "cluster") == "cluster_redundant"

    def test_manual_bucket(self):
        from hft_platform.alpha.audit import _classify_kill_reason

        assert _classify_kill_reason("operator decision", "manual") == "manual"

    def test_unknown_reason_falls_back_to_gate_class(self):
        from hft_platform.alpha.audit import _classify_kill_reason

        assert _classify_kill_reason("totally novel failure mode", "pre_screen") == "screener_other"
        assert _classify_kill_reason("totally novel failure mode", "C") == "other"


class TestLogKill:
    """log_kill() mirrors log_promotion_result(): metrics best-effort, CH+fallback."""

    def setup_method(self):
        import hft_platform.alpha.audit as mod

        mod._ENABLED = None

    def test_log_kill_noop_when_disabled_still_increments_metrics(self):
        from hft_platform.alpha.audit import log_kill

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "0"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            # Should not raise; metrics counter increment is best-effort.
            result = log_kill(_make_kill_record())
            assert result is None

    def test_log_kill_rejects_non_kill_record(self):
        from hft_platform.alpha.audit import log_kill

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "0"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            try:
                log_kill({"alpha_id": "alpha1"})
            except TypeError as exc:
                assert "KillRecord" in str(exc)
            else:
                raise AssertionError("log_kill should reject non-KillRecord input")

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_kill_inserts_row_when_enabled(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        from hft_platform.alpha.audit import log_kill

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            record = _make_kill_record()
            log_kill(record)

        mock_client.insert.assert_called_once()
        call_args = mock_client.insert.call_args
        assert call_args[0][0] == "audit.alpha_kill_ledger"
        row = call_args[0][1][0]
        assert row[0] == record.kill_id()  # kill_id
        assert row[2] == "alpha1"  # alpha_id
        assert row[3] == "C"  # gate

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_kill_fails_silently(self, mock_get_client):
        mock_get_client.side_effect = ConnectionError("no CH")

        from hft_platform.alpha.audit import log_kill

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            # Should not raise — falls back to local jsonl via _write_fallback.
            result = log_kill(_make_kill_record())
            assert result is None

    @patch("hft_platform.alpha.audit._get_client")
    def test_log_kill_killed_at_zero_filled_with_now(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        from hft_platform.alpha.audit import log_kill

        with patch.dict(os.environ, {"HFT_ALPHA_AUDIT_ENABLED": "1"}, clear=False):
            import hft_platform.alpha.audit as mod

            mod._ENABLED = None
            log_kill(_make_kill_record(killed_at=0))

        row = mock_client.insert.call_args[0][1][0]
        assert row[1] > 0, "killed_at=0 must be replaced with timebase.now_ns()"
