"""Tests that NotificationDispatcher still works after AlertRouter rewire."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_sender() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def mock_router() -> AsyncMock:
    router = AsyncMock()
    router.emit = AsyncMock()
    return router


@pytest.fixture
def dispatcher_with_router(mock_sender, mock_router):
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    d = NotificationDispatcher(sender=mock_sender)
    d._alert_router = mock_router
    return d


@pytest.mark.asyncio
async def test_notify_halt_emits_fatal_alert(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_halt(reason="risk limit")
    mock_router.emit.assert_awaited_once()
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.FATAL
    assert alert.category == "risk"
    assert alert.dedup_key == "halt"


@pytest.mark.asyncio
async def test_notify_daily_loss_emits_fatal_alert(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_daily_loss(pnl_ntd=-50000, limit_ntd=-40000)
    mock_router.emit.assert_awaited_once()
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.FATAL
    assert alert.category == "risk"
    assert alert.dedup_key == "daily_loss"


@pytest.mark.asyncio
async def test_notify_stormguard_change_emits_critical_for_storm(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_stormguard_change(old="NORMAL", new="STORM", reason="vol")
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.category == "risk"


@pytest.mark.asyncio
async def test_notify_stormguard_change_emits_warn_for_caution(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_stormguard_change(old="NORMAL", new="CAUTION", reason="vol")
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.WARN


@pytest.mark.asyncio
async def test_notify_stormguard_change_emits_critical_for_halt(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_stormguard_change(old="STORM", new="HALT", reason="loss")
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.CRITICAL


@pytest.mark.asyncio
async def test_notify_heartbeat_emits_info(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_heartbeat(
        autonomy_state="NORMAL", pnl_scaled=0, strategies_active=1, feed_status="ok"
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.INFO
    assert alert.category == "ops"
    assert alert.dedup_key == "heartbeat"


@pytest.mark.asyncio
async def test_notify_margin_critical_emits_critical(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_margin_critical(ratio=1.05, used=1000000, available=50000)
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.category == "risk"


@pytest.mark.asyncio
async def test_notify_margin_warning_emits_warn(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_margin_warning(ratio=0.85, used=850000, available=150000)
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.WARN
    assert alert.category == "risk"


@pytest.mark.asyncio
async def test_notify_flatten_result_emits_critical_when_failed(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_flatten_result(
        scope="all", fully_closed=2, partially_closed=0, failed=1, failed_symbols=["TXFD6"]
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.category == "execution"


@pytest.mark.asyncio
async def test_notify_flatten_result_emits_info_when_no_failures(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_flatten_result(
        scope="all", fully_closed=3, partially_closed=0, failed=0, failed_symbols=[]
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.INFO


@pytest.mark.asyncio
async def test_notify_reconnect_emits_warn(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_reconnect(count=2, flap_status="OK")
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.WARN
    assert alert.category == "broker"
    assert alert.dedup_key == "reconnect"


@pytest.mark.asyncio
async def test_notify_autonomy_transition_emits_critical_for_halt(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_autonomy_transition(
        scope="platform", from_mode="NORMAL", to_mode="HALT", reason="loss limit"
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.category == "ops"


@pytest.mark.asyncio
async def test_notify_autonomy_transition_emits_warn_for_non_halt(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_autonomy_transition(
        scope="platform", from_mode="NORMAL", to_mode="CAUTION", reason="volatility"
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.WARN


@pytest.mark.asyncio
async def test_notify_pre_market_pass_emits_info(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_pre_market_pass()
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.INFO
    assert alert.category == "ops"


@pytest.mark.asyncio
async def test_notify_pre_market_fail_emits_critical(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_pre_market_fail(failed_checks=["Redis timeout"])
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.category == "ops"


@pytest.mark.asyncio
async def test_notify_position_recovery_failed_emits_fatal(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_position_recovery_failed(source="dual", reason="timeout", mismatches=[])
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.FATAL
    assert alert.category == "position"


@pytest.mark.asyncio
async def test_notify_canary_action_rollback_emits_critical(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_canary_action(alpha_id="r47", action="rolled_back", reason="sharpe drop")
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.CRITICAL
    assert alert.category == "ops"


@pytest.mark.asyncio
async def test_notify_canary_action_graduated_emits_info(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_canary_action(alpha_id="r47", action="graduated", reason="all gates passed")
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.INFO


@pytest.mark.asyncio
async def test_notify_backup_success_emits_info(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_backup_success(
        date_str="2026-04-15", size_mb=512.0, duration_s=3.2, retained_count=7
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.INFO
    assert alert.category == "infra"


@pytest.mark.asyncio
async def test_notify_backup_failed_emits_warn(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_backup_failed(
        date_str="2026-04-15", error="disk full", last_success_date="2026-04-14"
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.WARN
    assert alert.category == "infra"


@pytest.mark.asyncio
async def test_notify_daily_report_emits_info(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_daily_report(
        date_str="2026-04-15",
        pnl_ntd=5000,
        buys=10,
        sells=10,
        fills=20,
        position_status="FLAT",
        reconciliation_status="OK",
        latency_p95_ms=1.5,
        reconnect_count=0,
        storm_guard_state="NORMAL",
        memory_gb=0.9,
        memory_max_gb=1.0,
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.INFO
    assert alert.category == "ops"


@pytest.mark.asyncio
async def test_notify_reconciliation_mismatch_emits_warn(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_reconciliation_mismatch(platform_pnl=12000, broker_pnl=11500, ch_pnl=12100)
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.WARN
    assert alert.category == "position"


# ---------------------------------------------------------------------------
# Legacy fallback path (no router)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_to_legacy_when_no_router(mock_sender):
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    d = NotificationDispatcher(sender=mock_sender)
    await d.notify_halt(reason="test")
    mock_sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallback_non_critical_when_no_router(mock_sender):
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    d = NotificationDispatcher(sender=mock_sender)
    await d.notify_heartbeat(autonomy_state="NORMAL", pnl_scaled=0, strategies_active=1, feed_status="ok")
    mock_sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_not_called_in_legacy_mode(mock_sender):
    """No _alert_router set — sender should be called, not a router."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    d = NotificationDispatcher(sender=mock_sender)
    assert d._alert_router is None
    await d.notify_reconnect(count=1, flap_status="OK")
    mock_sender.send.assert_awaited_once()
