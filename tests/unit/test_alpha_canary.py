"""Tests for hft_platform.alpha.canary module."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from hft_platform.alpha.canary import CanaryMonitor, CanaryStatus


def _write_canary_yaml(
    path: Path,
    alpha_id: str = "ofi_mc",
    weight: float = 0.02,
    enabled: bool = True,
    max_slippage: float = 3.0,
    max_dd_contrib: float = 0.02,
    max_error_rate: float = 0.01,
    sharpe_oos: float = 1.5,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "alpha_id": alpha_id,
        "enabled": enabled,
        "weight": weight,
        "owner": "test",
        "guardrails": {
            "max_live_slippage_bps": max_slippage,
            "max_live_drawdown_contribution": max_dd_contrib,
            "max_execution_error_rate": max_error_rate,
        },
        "rollback": {
            "trigger": {
                "live_slippage_bps_gt": max_slippage,
                "live_drawdown_contribution_gt": max_dd_contrib,
                "execution_error_rate_gt": max_error_rate,
            },
            "action": {"set_weight_to_zero": True, "open_incident": True},
        },
        "scorecard_snapshot": {"sharpe_oos": sharpe_oos},
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


class TestCanaryMonitorLoadActive:
    def test_no_dir(self, tmp_path: Path):
        monitor = CanaryMonitor(promotions_dir=str(tmp_path / "nonexistent"))
        assert monitor.load_active_canaries() == []

    def test_discovers_enabled_yaml(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "20260218" / "ofi_mc.yaml")
        _write_canary_yaml(promo_dir / "20260218" / "disabled.yaml", alpha_id="disabled", enabled=False)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        canaries = monitor.load_active_canaries()
        assert len(canaries) == 1
        assert canaries[0]["alpha_id"] == "ofi_mc"

    def test_multiple_canaries(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "a.yaml", alpha_id="alpha_a")
        _write_canary_yaml(promo_dir / "b.yaml", alpha_id="alpha_b")

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        canaries = monitor.load_active_canaries()
        assert len(canaries) == 2


class TestCanaryEvaluate:
    def test_not_found(self, tmp_path: Path):
        monitor = CanaryMonitor(promotions_dir=str(tmp_path / "empty"))
        status = monitor.evaluate("unknown", {})
        assert status.state == "not_found"

    def test_hold_when_all_checks_pass(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "ofi.yaml")

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        status = monitor.evaluate("ofi_mc", {
            "slippage_bps": 1.0,
            "drawdown_contribution": 0.005,
            "execution_error_rate": 0.001,
            "sessions_live": 3,
        })
        assert status.state == "canary"
        assert status.current_weight == 0.02
        assert "All checks passed" in status.reason

    def test_rollback_on_slippage(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "ofi.yaml", max_slippage=3.0)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        status = monitor.evaluate("ofi_mc", {
            "slippage_bps": 5.0,
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.0,
            "sessions_live": 0,
        })
        assert status.state == "rolled_back"
        assert "slippage_bps" in status.reason

    def test_rollback_on_drawdown(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "ofi.yaml", max_dd_contrib=0.02)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        status = monitor.evaluate("ofi_mc", {
            "slippage_bps": 1.0,
            "drawdown_contribution": 0.05,
            "execution_error_rate": 0.0,
            "sessions_live": 0,
        })
        assert status.state == "rolled_back"
        assert "drawdown_contribution" in status.reason

    def test_rollback_on_error_rate(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "ofi.yaml", max_error_rate=0.01)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        status = monitor.evaluate("ofi_mc", {
            "slippage_bps": 1.0,
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.05,
            "sessions_live": 0,
        })
        assert status.state == "rolled_back"
        assert "execution_error_rate" in status.reason

    def test_escalation_with_good_sharpe(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "ofi.yaml", weight=0.02, sharpe_oos=1.5)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        monitor.escalation_sessions = 10
        monitor.sharpe_ratio = 0.8

        status = monitor.evaluate("ofi_mc", {
            "slippage_bps": 1.0,
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.0,
            "sessions_live": 12,
            "sharpe_live": 1.5,  # >= 1.5 * 0.8 = 1.2
        })
        assert status.state == "escalated"
        assert "0.02" in status.reason
        assert "0.05" in status.reason  # next tier

    def test_hold_when_sharpe_too_low(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "ofi.yaml", weight=0.02, sharpe_oos=2.0)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        monitor.escalation_sessions = 10
        monitor.sharpe_ratio = 0.8

        status = monitor.evaluate("ofi_mc", {
            "slippage_bps": 1.0,
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.0,
            "sessions_live": 15,
            "sharpe_live": 1.0,  # < 2.0 * 0.8 = 1.6
        })
        assert status.state == "canary"

    def test_graduation_at_max_tier(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "ofi.yaml", weight=0.10, sharpe_oos=1.5)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        monitor.escalation_sessions = 5

        status = monitor.evaluate("ofi_mc", {
            "slippage_bps": 1.0,
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.0,
            "sessions_live": 10,
            "sharpe_live": 2.0,
        })
        assert status.state == "graduated"


class TestCanaryApplyDecision:
    def test_apply_rollback(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        yaml_path = _write_canary_yaml(promo_dir / "ofi.yaml", weight=0.05)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        status = CanaryStatus(
            alpha_id="ofi_mc",
            current_weight=0.05,
            state="rolled_back",
            reason="slippage exceeded",
            checks={},
        )
        monitor.apply_decision(status)

        updated = yaml.safe_load(yaml_path.read_text())
        assert updated["weight"] == 0.0
        assert updated["enabled"] is False

    def test_apply_escalation(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        yaml_path = _write_canary_yaml(promo_dir / "ofi.yaml", weight=0.02)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        status = CanaryStatus(
            alpha_id="ofi_mc",
            current_weight=0.02,
            state="escalated",
            reason="escalating",
            checks={},
        )
        monitor.apply_decision(status)

        updated = yaml.safe_load(yaml_path.read_text())
        assert updated["weight"] == 0.05  # next tier after 0.02

    def test_apply_graduation(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        yaml_path = _write_canary_yaml(promo_dir / "ofi.yaml", weight=0.10)

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        status = CanaryStatus(
            alpha_id="ofi_mc",
            current_weight=0.10,
            state="graduated",
            reason="graduated",
            checks={},
        )
        monitor.apply_decision(status)

        updated = yaml.safe_load(yaml_path.read_text())
        assert updated["weight"] == 0.10
        assert "rollback" not in updated  # removed on graduation

    def test_apply_hold_is_noop(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        yaml_path = _write_canary_yaml(promo_dir / "ofi.yaml", weight=0.02)
        original = yaml_path.read_text()

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        status = CanaryStatus(
            alpha_id="ofi_mc",
            current_weight=0.02,
            state="canary",
            reason="holding",
            checks={},
        )
        monitor.apply_decision(status)
        assert yaml_path.read_text() == original

    def test_apply_not_found(self, tmp_path: Path):
        monitor = CanaryMonitor(promotions_dir=str(tmp_path / "empty"))
        status = CanaryStatus(
            alpha_id="unknown",
            current_weight=0.0,
            state="rolled_back",
            reason="test",
            checks={},
        )
        # Should not raise
        monitor.apply_decision(status)


class TestCanaryStatusToDict:
    def test_to_dict(self):
        status = CanaryStatus(
            alpha_id="alpha1",
            current_weight=0.05,
            state="canary",
            reason="ok",
            checks={"a": 1},
        )
        d = status.to_dict()
        assert d["alpha_id"] == "alpha1"
        assert d["state"] == "canary"
        assert d["checks"] == {"a": 1}


class TestNextTierWeight:
    def test_tier_progression(self):
        monitor = CanaryMonitor()
        assert monitor._next_tier_weight(0.01) == 0.02
        assert monitor._next_tier_weight(0.02) == 0.05
        assert monitor._next_tier_weight(0.05) == 0.07
        assert monitor._next_tier_weight(0.07) == 0.10
        assert monitor._next_tier_weight(0.10) is None
        assert monitor._next_tier_weight(0.15) is None
