"""Coverage tests for notifications/alert_router.py — uncovered silence, escalation, tick paths."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import yaml

from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule


def _make_alert(
    *,
    alert_id: str = "a-001",
    severity: AlertSeverity = AlertSeverity.WARN,
    category: str = "feed",
    source: str = "test",
    dedup_key: str | None = None,
    ts_ns: int = 1_000_000_000_000_000_000,
) -> Alert:
    return Alert(
        alert_id=alert_id,
        severity=severity,
        category=category,
        source=source,
        title="Test alert",
        detail="Test detail",
        ts_ns=ts_ns,
        dedup_key=dedup_key,
        metadata=None,
    )


@pytest.fixture
def mock_telegram() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def mock_webhook() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def router(mock_telegram, mock_webhook, tmp_path):
    """Router with no silence config file."""
    from hft_platform.notifications.alert_router import AlertRouter

    return AlertRouter(
        telegram_sender=mock_telegram,
        webhook_sender=mock_webhook,
        silence_config_path=tmp_path / "nonexistent.yaml",
    )


# ---------------------------------------------------------------------------
# _load_silence_rules — from YAML file (lines 64, 69-81)
# ---------------------------------------------------------------------------


class TestLoadSilenceRules:
    def test_loads_silence_rules_from_yaml(self, mock_telegram, tmp_path):
        """Load silence rules from a valid YAML config."""
        from hft_platform.notifications.alert_router import AlertRouter

        config_path = tmp_path / "silence.yaml"
        config_path.write_text(
            yaml.dump(
                {
                    "rules": [
                        {
                            "rule_id": "r1",
                            "category": "feed",
                            "source": "test",
                            "severity_max": "WARN",
                            "reason": "maintenance",
                        },
                        {
                            "rule_id": "r2",
                            "category": "risk",
                            "severity_max": "CRITICAL",
                            "start_ns": 100,
                            "end_ns": 200,
                            "reason": "deploy",
                        },
                    ]
                }
            )
        )
        router = AlertRouter(
            telegram_sender=mock_telegram,
            silence_config_path=config_path,
        )
        assert "r1" in router._silence_rules
        assert "r2" in router._silence_rules
        assert router._silence_rules["r1"].category == "feed"
        assert router._silence_rules["r2"].severity_max == AlertSeverity.CRITICAL

    def test_handles_corrupt_yaml_gracefully(self, mock_telegram, tmp_path):
        """Corrupt YAML file should not raise."""
        from hft_platform.notifications.alert_router import AlertRouter

        config_path = tmp_path / "bad.yaml"
        config_path.write_text("{{not valid yaml::::")
        router = AlertRouter(
            telegram_sender=mock_telegram,
            silence_config_path=config_path,
        )
        assert len(router._silence_rules) == 0

    def test_handles_missing_file(self, mock_telegram, tmp_path):
        """Missing file is silently skipped."""
        from hft_platform.notifications.alert_router import AlertRouter

        router = AlertRouter(
            telegram_sender=mock_telegram,
            silence_config_path=tmp_path / "nope.yaml",
        )
        assert len(router._silence_rules) == 0

    def test_empty_yaml_no_rules(self, mock_telegram, tmp_path):
        """Empty YAML file produces no rules."""
        from hft_platform.notifications.alert_router import AlertRouter

        config_path = tmp_path / "empty.yaml"
        config_path.write_text("")
        router = AlertRouter(
            telegram_sender=mock_telegram,
            silence_config_path=config_path,
        )
        assert len(router._silence_rules) == 0


# ---------------------------------------------------------------------------
# remove_silence — returns bool (lines 91-93)
# ---------------------------------------------------------------------------


class TestRemoveSilence:
    def test_remove_nonexistent_returns_false(self, router):
        assert router.remove_silence("nonexistent") is False

    def test_remove_existing_returns_true(self, router):
        rule = SilenceRule(
            rule_id="s-1",
            category="feed",
            source=None,
            severity_max=AlertSeverity.WARN,
            start_ns=0,
            end_ns=0,
            reason="test",
        )
        router.add_silence(rule)
        assert router.remove_silence("s-1") is True


# ---------------------------------------------------------------------------
# emit — CRITICAL without webhook (line 141->143)
# ---------------------------------------------------------------------------


class TestEmitCriticalNoWebhook:
    @pytest.mark.asyncio
    async def test_critical_without_webhook(self, mock_telegram, tmp_path):
        """Critical alert with no webhook only sends Telegram."""
        from hft_platform.notifications.alert_router import AlertRouter

        router = AlertRouter(
            telegram_sender=mock_telegram,
            webhook_sender=None,
            silence_config_path=tmp_path / "nope.yaml",
        )
        alert = _make_alert(severity=AlertSeverity.CRITICAL)
        await router.emit(alert)
        mock_telegram.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# flush_info_batch — more than 10 items (lines 165-170)
# ---------------------------------------------------------------------------


class TestFlushInfoBatchTruncation:
    @pytest.mark.asyncio
    async def test_flush_with_more_than_10_alerts(self, router, mock_telegram):
        """Info batch with >10 alerts should show truncation message."""
        for i in range(15):
            alert = _make_alert(
                severity=AlertSeverity.INFO,
                alert_id=f"info-{i}",
                dedup_key=f"unique-{i}",
            )
            await router.emit(alert)

        await router.flush_info_batch()
        msg = mock_telegram.send.call_args.args[0]
        assert "15" in msg
        assert "5 more" in msg


# ---------------------------------------------------------------------------
# acknowledge — delegation to escalation tracker (lines 176-178)
# ---------------------------------------------------------------------------


class TestAcknowledge:
    def test_acknowledge_tracked_alert(self, router):
        alert = _make_alert(severity=AlertSeverity.CRITICAL, alert_id="c-1")
        router._escalation.track(alert)
        result = router.acknowledge("c-1")
        assert result is True

    def test_acknowledge_untracked_alert(self, router):
        result = router.acknowledge("not-tracked")
        assert result is False


# ---------------------------------------------------------------------------
# tick — periodic maintenance (lines 190-210)
# ---------------------------------------------------------------------------


class TestTick:
    @pytest.mark.asyncio
    async def test_tick_flushes_info_batch(self, router, mock_telegram):
        """tick() should flush accumulated INFO alerts."""
        alert = _make_alert(severity=AlertSeverity.INFO, alert_id="tick-info")
        await router.emit(alert)
        assert mock_telegram.send.await_count == 0

        await router.tick(now_ns=2_000_000_000_000_000_000)
        # INFO batch should have been flushed
        mock_telegram.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_tick_with_aggregator_summaries(self, router, mock_telegram):
        """tick() sends suppression summaries from aggregator."""
        # Simulate dedup: same dedup_key, different alert_ids
        a1 = _make_alert(dedup_key="dup", ts_ns=1_000_000_000_000_000_000, severity=AlertSeverity.WARN)
        a2 = _make_alert(
            dedup_key="dup", ts_ns=1_000_000_000_000_000_001, alert_id="a-002", severity=AlertSeverity.WARN
        )
        await router.emit(a1)
        await router.emit(a2)

        # Flush expired with far-future time
        await router.tick(now_ns=9_999_999_999_999_999_999)

    @pytest.mark.asyncio
    async def test_tick_fires_due_escalations(self, router, mock_telegram, mock_webhook):
        """tick() re-sends alerts that are due for escalation."""
        # Track an alert with a very old timestamp so escalation is immediately due
        alert = _make_alert(
            severity=AlertSeverity.CRITICAL,
            alert_id="esc-1",
            ts_ns=100_000_000_000,  # Very old timestamp
        )
        router._escalation.track(alert)

        # Call tick with a far-future time so the escalation interval is exceeded
        await router.tick(now_ns=999_999_999_999_999_999)
        # Should have sent via _send_critical (telegram + webhook)
        assert mock_telegram.send.await_count >= 1


# ---------------------------------------------------------------------------
# _format_alert — static method (lines 146-161)
# ---------------------------------------------------------------------------


class TestFormatAlert:
    def test_format_info(self):
        from hft_platform.notifications.alert_router import AlertRouter

        alert = _make_alert(severity=AlertSeverity.INFO)
        msg = AlertRouter._format_alert(alert)
        assert "[INFO]" in msg
        assert "Test alert" in msg

    def test_format_fatal(self):
        from hft_platform.notifications.alert_router import AlertRouter

        alert = _make_alert(severity=AlertSeverity.FATAL)
        msg = AlertRouter._format_alert(alert)
        assert "[FATAL]" in msg
