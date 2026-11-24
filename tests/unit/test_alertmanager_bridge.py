"""Tests for Alertmanager → Telegram bridge."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.notifications.alertmanager_bridge import (
    AlertmanagerBridge,
    format_alert_message,
)
from hft_platform.notifications.telegram import TelegramSender

# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

SAMPLE_FIRING: dict = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "FeedGapCritical", "severity": "critical"},
            "annotations": {
                "summary": "Market Data Gap Detected",
                "description": "No feed events for >5s during uptime.",
            },
            "startsAt": "2026-03-27T09:00:00Z",
        }
    ],
}

SAMPLE_RESOLVED: dict = {
    "status": "resolved",
    "alerts": [
        {
            "status": "resolved",
            "labels": {"alertname": "FeedGapCritical", "severity": "critical"},
            "annotations": {"summary": "Market Data Gap Detected"},
            "startsAt": "2026-03-27T09:00:00Z",
            "endsAt": "2026-03-27T09:05:00Z",
        }
    ],
}


# ---------------------------------------------------------------------------
# Unit tests: format_alert_message
# ---------------------------------------------------------------------------


class TestFormatAlertMessage:
    def test_firing_alert_contains_name_and_severity(self) -> None:
        msg = format_alert_message(SAMPLE_FIRING)
        assert "FeedGapCritical" in msg
        assert "critical" in msg.lower()
        assert "FIRING" in msg or "firing" in msg.lower()

    def test_resolved_alert_contains_resolved_tag(self) -> None:
        msg = format_alert_message(SAMPLE_RESOLVED)
        assert "RESOLVED" in msg or "resolved" in msg.lower()

    def test_empty_alerts_returns_empty_string(self) -> None:
        msg = format_alert_message({"status": "firing", "alerts": []})
        assert msg == ""

    def test_multiple_alerts_all_included(self) -> None:
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "A", "severity": "warning"},
                    "annotations": {"summary": "First"},
                },
                {
                    "status": "firing",
                    "labels": {"alertname": "B", "severity": "critical"},
                    "annotations": {"summary": "Second"},
                },
            ],
        }
        msg = format_alert_message(payload)
        assert "A" in msg
        assert "B" in msg

    def test_firing_includes_description_when_present(self) -> None:
        msg = format_alert_message(SAMPLE_FIRING)
        assert "No feed events" in msg

    def test_resolved_no_description_still_formats(self) -> None:
        msg = format_alert_message(SAMPLE_RESOLVED)
        assert "Market Data Gap Detected" in msg

    def test_missing_alerts_key_returns_empty(self) -> None:
        msg = format_alert_message({})
        assert msg == ""

    def test_warning_severity_included(self) -> None:
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "SlowFeed", "severity": "warning"},
                    "annotations": {"summary": "Feed latency high"},
                }
            ],
        }
        msg = format_alert_message(payload)
        assert "SlowFeed" in msg
        assert "warning" in msg

    def test_distinguishing_label_is_shown_and_scrape_labels_are_not(self) -> None:
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "OrderOutcome",
                        "severity": "warning",
                        "job": "hft-engine",
                        "instance": "hft-engine:9090",
                        "outcome": "cancel_gave_up",
                    },
                    "annotations": {"summary": "orders ending badly"},
                }
            ],
        }
        msg = format_alert_message(payload)
        assert "outcome=cancel_gave_up" in msg
        assert "job=" not in msg
        assert "instance=" not in msg
        assert "hft-engine" not in msg
        assert "alertname=" not in msg
        assert "severity=" not in msg

    def test_synthetic_prometheus_labels_are_not_shown(self) -> None:
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "A",
                        "severity": "warning",
                        "alertstate": "firing",
                        "__name__": "ALERTS",
                        "side": "buy",
                    },
                    "annotations": {"summary": "s"},
                }
            ]
        }
        msg = format_alert_message(payload)
        assert "side=buy" in msg
        assert "alertstate" not in msg
        assert "__name__" not in msg

    def test_labels_are_sorted_and_comma_separated(self) -> None:
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "A", "severity": "warning", "reason": "r", "conn_id": "2", "strategy": "s"},
                    "annotations": {"summary": "s"},
                }
            ]
        }
        msg = format_alert_message(payload)
        assert "conn_id=2, reason=r, strategy=s" in msg

    def test_series_differing_only_by_a_label_read_differently(self) -> None:
        def _alert(outcome: str) -> dict:
            return {
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {"alertname": "OrderOutcome", "severity": "warning", "outcome": outcome},
                        "annotations": {"summary": "same summary"},
                    }
                ]
            }

        assert format_alert_message(_alert("rejected")) != format_alert_message(_alert("cancel_gave_up"))

    def test_label_values_are_html_escaped(self) -> None:
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "A", "severity": "warning", "reason": "<b>x</b> & y"},
                    "annotations": {"summary": "s"},
                }
            ]
        }
        msg = format_alert_message(payload)
        assert "reason=&lt;b&gt;x&lt;/b&gt; &amp; y" in msg
        assert "<b>x</b>" not in msg

    def test_no_label_line_when_only_skipped_labels_remain(self) -> None:
        # SAMPLE_FIRING carries only alertname + severity.
        msg = format_alert_message(SAMPLE_FIRING)
        assert "=" not in msg
        assert msg.split("\n\n") == [
            "\u26a0\ufe0f <b>[FIRING] FeedGapCritical</b>\n  Severity: critical\n  Market Data Gap Detected",
            "  No feed events for >5s during uptime.".replace(">", "&gt;"),
        ]


# ---------------------------------------------------------------------------
# Integration-style tests: AlertmanagerBridge HTTP server
# ---------------------------------------------------------------------------


WARNING_PAYLOAD: dict = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "SlowFeed", "severity": "warning"},
            "annotations": {"summary": "Feed latency high"},
        }
    ],
}


async def _post_webhook(bridge: AlertmanagerBridge, payload: dict) -> bytes:
    """POST *payload* to a live bridge on an ephemeral port; return the raw response."""
    bridge._server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
    port = bridge._server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps(payload).encode()
        writer.write((f"POST /webhook/alertmanager HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        writer.close()
        return response
    finally:
        bridge.stop()


def _mock_response(status: int) -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value="")
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(*responses: AsyncMock) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(side_effect=list(responses))
    session.close = AsyncMock()
    return session


class TestAlertmanagerBridgeServer:
    @pytest.mark.asyncio
    async def test_webhook_endpoint_accepts_post(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=True)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)

        bridge._server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = bridge._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps(SAMPLE_FIRING).encode()
            request = (f"POST /webhook/alertmanager HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()

            assert b"200 OK" in response
            mock_sender.send.assert_called_once()
            call_args = mock_sender.send.call_args
            assert "FeedGapCritical" in call_args[0][0]
            assert call_args[1]["critical"] is True
        finally:
            bridge.stop()

    @pytest.mark.asyncio
    async def test_healthz_returns_ok(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)
        bridge._server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = bridge._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /healthz HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()
            assert b"200 OK" in response
        finally:
            bridge.stop()

    @pytest.mark.asyncio
    async def test_unknown_path_returns_404(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)
        bridge._server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = bridge._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /nonexistent HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()
            assert b"404" in response
        finally:
            bridge.stop()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)
        bridge._server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = bridge._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = b"not valid json {"
            request = (f"POST /webhook/alertmanager HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()
            assert b"400" in response
        finally:
            bridge.stop()

    @pytest.mark.asyncio
    async def test_empty_alerts_no_send_called(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=True)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)
        bridge._server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = bridge._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"status": "firing", "alerts": []}).encode()
            request = (f"POST /webhook/alertmanager HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()

            assert b"200 OK" in response
            mock_sender.send.assert_not_called()
        finally:
            bridge.stop()

    @pytest.mark.asyncio
    async def test_warning_alert_is_sent_on_the_retrying_path(self) -> None:
        """Every forward retries (critical=True), not only severity=critical."""
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=True)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)

        response = await _post_webhook(bridge, WARNING_PAYLOAD)

        assert b"200 OK" in response
        mock_sender.send.assert_called_once()
        assert "SlowFeed" in mock_sender.send.call_args[0][0]
        assert mock_sender.send.call_args[1]["critical"] is True

    @pytest.mark.asyncio
    async def test_warning_alert_retries_a_transient_telegram_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Real TelegramSender, mocked session: 500 then 200 is delivered, answer 200."""
        monkeypatch.setattr(TelegramSender, "_RETRY_BACKOFF_S", 0.0)
        sender = TelegramSender(bot_token="TEST", chat_id="0", enabled=True, rate_limit_seconds=0.0)
        session = _mock_session(_mock_response(500), _mock_response(200))
        sender._session = session
        bridge = AlertmanagerBridge(port=0, sender=sender)

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientTimeout = MagicMock()
            response = await _post_webhook(bridge, WARNING_PAYLOAD)

        assert b"200 OK" in response
        assert session.post.call_count == 2

    @pytest.mark.asyncio
    async def test_webhook_returns_503_when_send_fails(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=False)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)

        response = await _post_webhook(bridge, WARNING_PAYLOAD)

        assert response.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
        assert b"accepted" not in response

    @pytest.mark.asyncio
    async def test_webhook_returns_503_when_every_telegram_attempt_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(TelegramSender, "_RETRY_BACKOFF_S", 0.0)
        sender = TelegramSender(bot_token="TEST", chat_id="0", enabled=True, rate_limit_seconds=0.0)
        attempts = TelegramSender._MAX_CRITICAL_RETRIES + 1
        session = _mock_session(*[_mock_response(500) for _ in range(attempts)])
        sender._session = session
        bridge = AlertmanagerBridge(port=0, sender=sender)

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientTimeout = MagicMock()
            response = await _post_webhook(bridge, WARNING_PAYLOAD)

        assert response.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
        assert session.post.call_count == attempts

    @pytest.mark.asyncio
    async def test_webhook_returns_200_when_send_succeeds(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=True)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)

        response = await _post_webhook(bridge, WARNING_PAYLOAD)

        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"accepted" in response

    @pytest.mark.asyncio
    async def test_webhook_returns_200_for_empty_alerts_even_when_sender_would_fail(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=False)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)

        response = await _post_webhook(bridge, {"status": "firing", "alerts": []})

        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        mock_sender.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_forwarded_log_keeps_sent_false_on_failed_send(self, module_log_sink) -> None:
        from hft_platform.notifications import alertmanager_bridge as br_mod

        events = module_log_sink(br_mod)
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=False)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)

        await _post_webhook(bridge, WARNING_PAYLOAD)

        forwarded = [e for e in events if e["event"] == "alertmanager_webhook_forwarded"]
        assert len(forwarded) == 1
        assert forwarded[0]["sent"] is False

    @pytest.mark.asyncio
    async def test_forwarded_log_includes_alertname_severity_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Post-Cycle-2: bridge must emit alertname/severity/status so
        post-hoc audits can identify the rule that drove a TG burst without
        firing-time correlation."""
        from unittest.mock import MagicMock

        from hft_platform.notifications import alertmanager_bridge as br_mod

        captured: list[dict] = []

        def _capture_info(event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs})

        fake_logger = MagicMock()
        fake_logger.info = _capture_info
        monkeypatch.setattr(br_mod, "logger", fake_logger)

        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=True)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)
        bridge._server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = bridge._server.sockets[0].getsockname()[1]

        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "MonitorLiveDrop", "severity": "critical"},
                    "annotations": {"summary": "Live monitor drop"},
                },
                {
                    "status": "firing",
                    "labels": {"alertname": "FeedGapCritical", "severity": "critical"},
                    "annotations": {"summary": "feed gap"},
                },
            ],
        }
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps(payload).encode()
            req = (f"POST /webhook/alertmanager HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
            writer.write(req)
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()

            forwarded = [r for r in captured if r["event"] == "alertmanager_webhook_forwarded"]
            assert forwarded, "no alertmanager_webhook_forwarded log emitted"
            rec = forwarded[-1]
            assert rec["alert_count"] == 2
            assert rec["alertnames"] == ["FeedGapCritical", "MonitorLiveDrop"]
            assert rec["severities"] == ["critical"]
            assert rec["statuses"] == ["firing"]
        finally:
            bridge.stop()
