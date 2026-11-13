"""Alertmanager webhook → Telegram bridge.

Lightweight raw-asyncio HTTP server (same pattern as HealthServer).
Receives Alertmanager webhook POSTs and forwards to Telegram.

Port configured via ``HFT_ALERT_BRIDGE_PORT`` (default 8081).
"""

from __future__ import annotations

import asyncio
import json
import os
from html import escape
from typing import Any

from structlog import get_logger

from hft_platform.notifications.telegram import TelegramSender

logger = get_logger("notifications.alertmanager_bridge")

_DEFAULT_PORT = 8081

# Labels that never distinguish one series from another in a Telegram line:
# alertname/severity are already printed, job/instance are the scrape target
# (the same for every alert from this stack), alertstate/__name__ are
# synthetic labels added by Prometheus' ALERTS series.
_SKIPPED_LABELS: frozenset[str] = frozenset({"alertname", "severity", "job", "instance", "alertstate", "__name__"})


def format_alert_message(payload: dict[str, Any]) -> str:
    """Convert Alertmanager webhook payload to Telegram HTML message.

    Returns an empty string when there are no alerts to report.
    """
    alerts = payload.get("alerts", [])
    if not alerts:
        return ""

    lines: list[str] = []
    for alert in alerts:
        status = alert.get("status", "unknown").upper()
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        name = labels.get("alertname", "unknown")
        severity = labels.get("severity", "unknown")
        summary = annotations.get("summary", "")
        description = annotations.get("description", "")

        icon = "\u26a0\ufe0f" if status == "FIRING" else "\u2705"
        lines.append(
            f"{icon} <b>[{escape(status)}] {escape(name)}</b>\n  Severity: {escape(severity)}\n  {escape(summary)}"
        )
        if description:
            lines.append(f"  {escape(description)}")
        # Series that differ only by a label (outcome, reason, side, conn_id,
        # strategy, ...) would otherwise read identically.
        distinguishing = ", ".join(
            f"{escape(str(key))}={escape(str(value))}"
            for key, value in sorted(labels.items())
            if key not in _SKIPPED_LABELS
        )
        if distinguishing:
            lines.append(f"  {distinguishing}")

    return "\n\n".join(lines)


class AlertmanagerBridge:
    """Raw-asyncio HTTP server that receives Alertmanager webhooks.

    Endpoints:
        ``POST /webhook/alertmanager`` — Receive and forward alert payloads.
        ``GET  /healthz``             — Liveness probe; always 200.
    """

    __slots__ = ("_port", "_sender", "_server")

    def __init__(
        self,
        *,
        port: int = 0,
        sender: TelegramSender | None = None,
    ) -> None:
        self._port: int = port or int(os.getenv("HFT_ALERT_BRIDGE_PORT", str(_DEFAULT_PORT)))
        self._sender: TelegramSender = sender if sender is not None else TelegramSender(enabled=True)
        self._server: asyncio.Server | None = None

    # -- HTTP protocol handler -----------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single HTTP connection."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            parts = request_line.decode("utf-8", errors="replace").strip().split()
            method = parts[0] if parts else ""
            path = parts[1].split("?")[0] if len(parts) > 1 else ""

            content_length = 0
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if header_line in (b"\r\n", b"\n", b""):
                    break
                if header_line.lower().startswith(b"content-length:"):
                    try:
                        content_length = int(header_line.split(b":")[1].strip())
                    except (ValueError, IndexError):
                        pass

            _MAX_BODY = 1_048_576  # 1 MB
            body = b""
            if content_length > _MAX_BODY:
                await self._send_response(writer, 413, b'{"error":"payload_too_large"}')
                return
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

            if method == "POST" and path == "/webhook/alertmanager":
                await self._handle_webhook(body, writer)
            elif method == "GET" and path == "/healthz":
                await self._send_response(writer, 200, b'{"status":"ok"}')
            else:
                await self._send_response(writer, 404, b'{"error":"not_found"}')

        except (asyncio.TimeoutError, ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("alertmanager_bridge_handle_error")
            try:
                await self._send_response(writer, 500, b'{"error":"internal_server_error"}')
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_webhook(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        """Parse payload, format message, and forward to Telegram."""
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            await self._send_response(writer, 400, b'{"error":"invalid_json"}')
            return

        msg = format_alert_message(payload)
        if msg:
            alerts = payload.get("alerts", [])
            # Every forward takes the retrying path (``critical=True``: bounded
            # retries with backoff on transient errors, no client-side throttle
            # drop), not only severity=critical ones. A single un-retried attempt
            # silently lost a BrokerPathSilent page. ``critical`` changes only
            # retry/throttle behaviour in TelegramSender, never the text.
            sent = await self._sender.send(msg, critical=True)
            # Emit alertname/severity/status so post-hoc audits can identify
            # which rule(s) drove a telegram burst without re-deriving from
            # firing-time correlation. Sorted+deduped to keep log line bounded
            # even on grouped alerts.
            alertnames = sorted({a.get("labels", {}).get("alertname", "?") for a in alerts})
            severities = sorted({a.get("labels", {}).get("severity", "?") for a in alerts})
            statuses = sorted({a.get("status", "?") for a in alerts})
            logger.info(
                "alertmanager_webhook_forwarded",
                alert_count=len(alerts),
                alertnames=alertnames,
                severities=severities,
                statuses=statuses,
                sent=sent,
            )
            if not sent:
                # Answer 503 so Alertmanager retries the notification instead
                # of treating the alert as delivered. Accepted trade-off: a
                # retried multi-chunk message (>4096 chars) may re-send chunks
                # that already went out, i.e. duplicate lines, never a loss.
                await self._send_response(writer, 503, b'{"error":"telegram_send_failed"}')
                return
        await self._send_response(writer, 200, b'{"status":"accepted"}')

    @staticmethod
    async def _send_response(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        """Write a minimal HTTP/1.1 response."""
        reasons = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            413: "Payload Too Large",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }
        reason = reasons.get(status, "Unknown")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(header.encode("utf-8") + body)
        await writer.drain()

    # -- Lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Start the bridge HTTP server and serve until cancelled."""
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                "0.0.0.0",
                self._port,  # nosec B104
            )
        except OSError as exc:
            logger.error("alertmanager_bridge_bind_failed", port=self._port, error=str(exc))
            return

        actual_port = self._server.sockets[0].getsockname()[1]
        logger.info("alertmanager_bridge_started", port=actual_port)
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("alertmanager_bridge_stopped")

    def stop(self) -> None:
        """Close the server."""
        if self._server is not None:
            self._server.close()

    async def close(self) -> None:
        """Stop server and release Telegram session."""
        self.stop()
        await self._sender.close()
