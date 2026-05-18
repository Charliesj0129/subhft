"""Tests for Alertmanager → Telegram bridge."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

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


# ---------------------------------------------------------------------------
# Integration-style tests: AlertmanagerBridge HTTP server
# ---------------------------------------------------------------------------


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
    async def test_warning_severity_not_critical(self) -> None:
        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=True)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)
        bridge._server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = bridge._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
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
            body = json.dumps(payload).encode()
            request = (f"POST /webhook/alertmanager HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()

            assert b"200 OK" in response
            mock_sender.send.assert_called_once()
            call_args = mock_sender.send.call_args
            # warning severity → critical=False
            assert call_args[1]["critical"] is False
        finally:
            bridge.stop()

    @pytest.mark.asyncio
    async def test_forwarded_log_includes_alertname_severity_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
