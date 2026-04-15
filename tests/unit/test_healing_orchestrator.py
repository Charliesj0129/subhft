"""Tests for HealingOrchestrator."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity
from hft_platform.notifications.alert import AlertSeverity


def _playbook_yaml(tmp_path: Path) -> Path:
    config = {"playbooks": {
        "feed_gap_short": {
            "match": {"category": "feed", "description_contains": "feed_gap"},
            "actions": [{"name": "mock_fix", "risk": "auto", "timeout_s": 5}],
            "cooldown_s": 60, "max_retries": 3,
        },
        "broker_disconnect_confirm": {
            "match": {"category": "broker", "description_contains": "disconnect"},
            "actions": [{"name": "alert_and_wait_approval", "risk": "confirm", "timeout_s": 5}],
            "cooldown_s": 300, "max_retries": 1,
        },
    }}
    path = tmp_path / "playbook.yaml"
    path.write_text(yaml.dump(config))
    return path


def _make_fault(*, category=FaultCategory.FEED, description="feed_gap on TMFD6"):
    return FaultEvent(
        fault_id="f-001", category=category, severity=FaultSeverity.DEGRADED,
        source="test", description=description,
        ts_ns=1_700_000_000_000_000_000, context={"symbol": "TMFD6"},
    )


class TestHealingOrchestrator:
    @pytest.mark.asyncio
    async def test_auto_action_executes(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        mock_action = AsyncMock()
        registry = ActionRegistry()
        registry.register("mock_fix", mock_action)
        playbook = HealingPlaybook(_playbook_yaml(tmp_path))
        orch = HealingOrchestrator(playbook=playbook, action_registry=registry, alert_callback=AsyncMock())
        result = await orch.handle_fault(_make_fault())
        assert result is not None
        assert result.success is True
        mock_action.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_matching_playbook_emits_alert(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        alert_cb = AsyncMock()
        orch = HealingOrchestrator(
            playbook=HealingPlaybook(_playbook_yaml(tmp_path)),
            action_registry=ActionRegistry(), alert_callback=alert_cb,
        )
        fault = FaultEvent(
            fault_id="f-999", category=FaultCategory.INFRA,
            severity=FaultSeverity.DEGRADED, source="test",
            description="unknown_infra_issue",
            ts_ns=1_700_000_000_000_000_000, context=None,
        )
        result = await orch.handle_fault(fault)
        assert result is None
        alert_cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_action_failure_stops_sequence(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        registry = ActionRegistry()
        registry.register("mock_fix", AsyncMock(side_effect=RuntimeError("broken")))
        orch = HealingOrchestrator(
            playbook=HealingPlaybook(_playbook_yaml(tmp_path)),
            action_registry=registry, alert_callback=AsyncMock(),
        )
        result = await orch.handle_fault(_make_fault())
        assert result is not None
        assert result.success is False

    @pytest.mark.asyncio
    async def test_confirm_action_emits_critical_alert(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        alert_cb = AsyncMock()
        orch = HealingOrchestrator(
            playbook=HealingPlaybook(_playbook_yaml(tmp_path)),
            action_registry=ActionRegistry(), alert_callback=alert_cb,
        )
        fault = FaultEvent(
            fault_id="f-002", category=FaultCategory.BROKER,
            severity=FaultSeverity.IMPAIRED, source="test",
            description="broker disconnect extended",
            ts_ns=1_700_000_000_000_000_000, context=None,
        )
        result = await orch.handle_fault(fault)
        assert result is not None
        assert result.pending_approval is True
        found_critical = any(
            call.args[0].severity == AlertSeverity.CRITICAL
            for call in alert_cb.call_args_list
        )
        assert found_critical

    @pytest.mark.asyncio
    async def test_result_fields_populated_on_success(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        registry = ActionRegistry()
        registry.register("mock_fix", AsyncMock())
        playbook = HealingPlaybook(_playbook_yaml(tmp_path))
        orch = HealingOrchestrator(playbook=playbook, action_registry=registry, alert_callback=AsyncMock())
        result = await orch.handle_fault(_make_fault())
        assert result is not None
        assert result.fault_id == "f-001"
        assert result.playbook_name == "feed_gap_short"
        assert result.actions_completed == 1
        assert result.actions_total == 1
        assert result.error is None
        assert result.pending_approval is False
        assert result.duration_ms >= 0.0

    @pytest.mark.asyncio
    async def test_action_failure_emits_fatal_alert(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        alert_cb = AsyncMock()
        registry = ActionRegistry()
        registry.register("mock_fix", AsyncMock(side_effect=RuntimeError("oops")))
        orch = HealingOrchestrator(
            playbook=HealingPlaybook(_playbook_yaml(tmp_path)),
            action_registry=registry, alert_callback=alert_cb,
        )
        result = await orch.handle_fault(_make_fault())
        assert result is not None
        assert result.error == "oops"
        found_fatal = any(
            call.args[0].severity == AlertSeverity.FATAL
            for call in alert_cb.call_args_list
        )
        assert found_fatal

    @pytest.mark.asyncio
    async def test_approve_clears_pending_fault(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        orch = HealingOrchestrator(
            playbook=HealingPlaybook(_playbook_yaml(tmp_path)),
            action_registry=ActionRegistry(), alert_callback=AsyncMock(),
        )
        fault = FaultEvent(
            fault_id="f-003", category=FaultCategory.BROKER,
            severity=FaultSeverity.IMPAIRED, source="test",
            description="broker disconnect extended",
            ts_ns=1_700_000_000_000_000_000, context=None,
        )
        await orch.handle_fault(fault)
        assert orch.approve("f-003") is True
        assert orch.approve("f-003") is False  # already removed

    @pytest.mark.asyncio
    async def test_reject_clears_pending_fault(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        orch = HealingOrchestrator(
            playbook=HealingPlaybook(_playbook_yaml(tmp_path)),
            action_registry=ActionRegistry(), alert_callback=AsyncMock(),
        )
        fault = FaultEvent(
            fault_id="f-004", category=FaultCategory.BROKER,
            severity=FaultSeverity.IMPAIRED, source="test",
            description="broker disconnect extended",
            ts_ns=1_700_000_000_000_000_000, context=None,
        )
        await orch.handle_fault(fault)
        assert orch.reject("f-004") is True
        assert orch.reject("f-004") is False

    @pytest.mark.asyncio
    async def test_unknown_action_name_skips_gracefully(self, tmp_path):
        """Action not in registry is skipped (logged as warning), does not raise."""
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook
        # Registry has no "mock_fix" registered — should skip and still return success
        orch = HealingOrchestrator(
            playbook=HealingPlaybook(_playbook_yaml(tmp_path)),
            action_registry=ActionRegistry(), alert_callback=AsyncMock(),
        )
        result = await orch.handle_fault(_make_fault())
        # Action skipped; completed=0 but loop finishes without error
        assert result is not None
        assert result.success is True
        assert result.actions_completed == 0

    @pytest.mark.asyncio
    async def test_action_timeout_is_treated_as_failure(self, tmp_path):
        """Action that exceeds timeout_s raises asyncio.TimeoutError → treated as failure."""
        import asyncio

        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook

        config = {"playbooks": {
            "feed_gap_timeout": {
                "match": {"category": "feed", "description_contains": "feed_gap"},
                "actions": [{"name": "slow_action", "risk": "auto", "timeout_s": 0.01}],
                "cooldown_s": 60, "max_retries": 1,
            },
        }}
        path = tmp_path / "timeout_playbook.yaml"
        path.write_text(yaml.dump(config))

        async def slow(*args, **kwargs):
            await asyncio.sleep(10)  # Much longer than 0.01s timeout

        registry = ActionRegistry()
        registry.register("slow_action", slow)
        orch = HealingOrchestrator(
            playbook=HealingPlaybook(path),
            action_registry=registry, alert_callback=AsyncMock(),
        )
        result = await orch.handle_fault(_make_fault())
        assert result is not None
        assert result.success is False
