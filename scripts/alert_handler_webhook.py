"""AlertManager webhook receiver — dispatches fired alerts to AlertMitigator.

Listens on HFT_ALERT_WEBHOOK_PORT (default 9095) for POST /alerts from
AlertManager and dispatches to the incident auto-mitigation framework.

Usage:
    python scripts/alert_handler_webhook.py
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from structlog import get_logger

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hft_platform.incident.auto_mitigation import AlertMitigator  # noqa: E402

logger = get_logger("alert_webhook")

_mitigator = AlertMitigator()


class AlertWebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for AlertManager webhook payloads."""

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/alerts":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid JSON"}')
            return

        results = _process_alerts(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(results).encode())

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress default stderr logging; use structlog instead
        pass


def _process_alerts(payload: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse AlertManager payload and dispatch to AlertMitigator."""
    alerts: list[dict[str, Any]]
    if isinstance(payload, list):
        alerts = payload
    elif isinstance(payload, dict):
        alerts = payload.get("alerts", [payload])
    else:
        return []

    results: list[dict[str, Any]] = []
    for alert in alerts:
        alert_name = alert.get("labels", {}).get("alertname", "unknown")
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        status = alert.get("status", "firing")

        if status != "firing":
            logger.info("Skipping resolved alert", alert_name=alert_name)
            results.append({"alert_name": alert_name, "status": "skipped_resolved"})
            continue

        mitigation, action_result = _mitigator.execute(
            alert_name=alert_name,
            labels=labels,
            annotations=annotations,
        )
        results.append({
            "alert_name": alert_name,
            "action_type": mitigation.action_type,
            "safe": mitigation.safe,
            "success": action_result.success if action_result else None,
            "message": action_result.message if action_result else None,
        })

    return results


def main() -> None:
    port = int(os.getenv("HFT_ALERT_WEBHOOK_PORT", "9095"))
    server = HTTPServer(("0.0.0.0", port), AlertWebhookHandler)
    logger.info("Alert webhook server starting", port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Alert webhook server shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
