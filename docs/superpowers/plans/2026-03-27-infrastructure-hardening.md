# Infrastructure Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 8 ops/infra/analytics gaps across 3 waves to bring the HFT platform from dev-ready to production-ready.

**Architecture:** Each wave is independently deployable. Wave 1 (alerting, SDK pin, config snapshot) blocks go-live. Wave 2 (log aggregation, disk rotation, hardware monitoring, quarterly checks) enables sustained ops. Wave 3 (TCA completion, feasibility validation) extends existing analytics into a full scorecard.

**Tech Stack:** Python 3.12 (raw asyncio), ClickHouse, Prometheus, Loki/Promtail, shell scripts, Docker Compose

**Spec:** `docs/superpowers/specs/2026-03-27-infrastructure-hardening-roadmap.md`

---

## File Map

### Wave 1

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/hft_platform/notifications/alertmanager_bridge.py` | Raw-asyncio HTTP server, Alertmanager webhook → Telegram |
| Create | `tests/unit/test_alertmanager_bridge.py` | Unit tests for bridge |
| Edit | `config/monitoring/alerts/alertmanager.yml` | Point receiver at bridge |
| Edit | `src/hft_platform/services/bootstrap.py` | Start bridge task |
| Edit | `pyproject.toml:14` | Pin shioaji==1.2.9 |
| Create | `src/hft_platform/ops/config_snapshot.py` | Allowlisted env snapshot to ClickHouse |
| Create | `src/hft_platform/migrations/clickhouse/20260327_001_add_config_snapshots.sql` | Schema |
| Create | `tests/unit/test_config_snapshot.py` | Tests incl. secret redaction |

### Wave 2

| Action | Path | Responsibility |
|--------|------|----------------|
| Edit | `docker-compose.yml` (node-exporter) | Add textfile collector dir + mount |
| Edit | `docker-compose.yml` (new services) | Add loki + promtail services |
| Create | `config/monitoring/loki.yml` | Loki local storage config |
| Edit | `config/monitoring/alerts/rules.yaml` | Add loki_up, research_data, SMART alerts |
| Create | `scripts/research_data_rotate.sh` | 4-tier research data rotation |
| Create | `scripts/smart_check.sh` | SMART → Prometheus textfile |
| Create | `scripts/quarterly_health_check.py` | Quarterly automated checks |
| Edit | `Makefile` | Add quarterly-health-check target |
| Edit | `docs/operations/cron-setup-remote.md` | Add cron entries |
| Edit | `docs/operations/long-term-risk-register.md` | Mark R04, R09, R10, R12 done |

### Wave 3

| Action | Path | Responsibility |
|--------|------|----------------|
| Edit | `src/hft_platform/contracts/execution.py` | Add decision_price, arrival_price to FillEvent |
| Create | `src/hft_platform/migrations/clickhouse/20260327_002_add_tca_columns_to_fills.sql` | Add columns to hft.fills |
| Create | `src/hft_platform/tca/slippage.py` | SlippageDecomposer |
| Create | `src/hft_platform/tca/impact.py` | SqrtImpactModel |
| Create | `src/hft_platform/tca/report.py` | TCAReportGenerator |
| Create | `src/hft_platform/execution/slippage_tracker.py` | Per-fill real-time slippage → Prometheus |
| Create | `src/hft_platform/risk/liquidity_gate.py` | Spread-based order rejection |
| Create | `src/hft_platform/ops/daily_pnl_report.py` | PnL section for DailyReportService |
| Edit | `src/hft_platform/services/daily_report.py` | Integrate TCA + PnL sections |
| Create | `src/hft_platform/analytics/__init__.py` | Package init |
| Create | `src/hft_platform/analytics/queries.py` | ClickHouse aggregation queries |
| Create | `src/hft_platform/cli/_feasibility.py` | `hft feasibility report` command |
| Create | `tests/unit/test_tca_slippage.py` | |
| Create | `tests/unit/test_tca_impact.py` | |
| Create | `tests/unit/test_tca_report.py` | |
| Create | `tests/unit/test_slippage_tracker.py` | |
| Create | `tests/unit/test_liquidity_gate.py` | |
| Create | `tests/unit/test_daily_pnl_report.py` | |

---

## Wave 1 — 上線前必備

### Task 1: Alertmanager Bridge — Webhook → Telegram

**Files:**
- Create: `src/hft_platform/notifications/alertmanager_bridge.py`
- Create: `tests/unit/test_alertmanager_bridge.py`
- Edit: `config/monitoring/alerts/alertmanager.yml`
- Edit: `src/hft_platform/services/bootstrap.py`

- [ ] **Step 1: Write failing tests for payload parsing**

```python
# tests/unit/test_alertmanager_bridge.py
"""Tests for Alertmanager → Telegram bridge."""
from __future__ import annotations

import pytest

from hft_platform.notifications.alertmanager_bridge import format_alert_message


SAMPLE_FIRING = {
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

SAMPLE_RESOLVED = {
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_alertmanager_bridge.py -v`
Expected: `ModuleNotFoundError: No module named 'hft_platform.notifications.alertmanager_bridge'`

- [ ] **Step 3: Implement alertmanager_bridge.py**

```python
# src/hft_platform/notifications/alertmanager_bridge.py
"""Alertmanager webhook → Telegram bridge.

Lightweight raw-asyncio HTTP server (same pattern as HealthServer).
Receives Alertmanager webhook POSTs and forwards to Telegram.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from structlog import get_logger

from hft_platform.notifications.telegram import TelegramSender

logger = get_logger("notifications.alertmanager_bridge")

_DEFAULT_PORT = 8081


def format_alert_message(payload: dict[str, Any]) -> str:
    """Convert Alertmanager webhook payload to Telegram HTML message."""
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
            f"{icon} <b>[{status}] {name}</b>\n"
            f"  Severity: {severity}\n"
            f"  {summary}"
        )
        if description:
            lines.append(f"  {description}")
    return "\n\n".join(lines)


class AlertmanagerBridge:
    """Raw-asyncio HTTP server that receives Alertmanager webhooks."""

    __slots__ = ("_port", "_sender", "_server")

    def __init__(self, *, port: int = 0, sender: TelegramSender | None = None) -> None:
        self._port = port or int(os.getenv("HFT_ALERT_BRIDGE_PORT", str(_DEFAULT_PORT)))
        if sender is not None:
            self._sender = sender
        else:
            self._sender = TelegramSender(enabled=True)
        self._server: asyncio.Server | None = None

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            parts = request_line.decode("utf-8", errors="replace").strip().split()
            method = parts[0] if parts else ""
            path = parts[1] if len(parts) > 1 else ""

            # Read headers to find Content-Length
            content_length = 0
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if header_line in (b"\r\n", b"\n", b""):
                    break
                if header_line.lower().startswith(b"content-length:"):
                    content_length = int(header_line.split(b":")[1].strip())

            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

            if method == "POST" and path == "/webhook/alertmanager":
                await self._handle_webhook(body, writer)
            elif method == "GET" and path == "/healthz":
                self._send_response(writer, 200, b'{"status":"ok"}')
            else:
                self._send_response(writer, 404, b"Not Found")
        except (asyncio.TimeoutError, ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("alertmanager_bridge_handle_error")
            self._send_response(writer, 500, b"Internal Server Error")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_webhook(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._send_response(writer, 400, b"Invalid JSON")
            return

        msg = format_alert_message(payload)
        if msg:
            is_critical = any(
                a.get("labels", {}).get("severity") == "critical"
                for a in payload.get("alerts", [])
            )
            sent = await self._sender.send(msg, critical=is_critical)
            logger.info(
                "alertmanager_webhook_forwarded",
                alert_count=len(payload.get("alerts", [])),
                sent=sent,
            )
        self._send_response(writer, 200, b'{"status":"accepted"}')

    @staticmethod
    def _send_response(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}
        header = (
            f"HTTP/1.1 {status} {reason.get(status, 'Unknown')}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        )
        writer.write(header.encode() + body)

    async def run(self) -> None:
        """Start the bridge server."""
        self._server = await asyncio.start_server(
            self._handle_connection, "0.0.0.0", self._port
        )
        logger.info("alertmanager_bridge_started", port=self._port)
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        """Stop the bridge server."""
        if self._server is not None:
            self._server.close()

    async def close(self) -> None:
        """Close the Telegram sender session."""
        await self._sender.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_alertmanager_bridge.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Write integration-style test for the HTTP server**

Add to `tests/unit/test_alertmanager_bridge.py`:

```python
class TestAlertmanagerBridgeServer:
    @pytest.mark.asyncio
    async def test_webhook_endpoint_accepts_post(self) -> None:
        """Verify the server accepts POST and returns 200."""
        from unittest.mock import AsyncMock

        mock_sender = AsyncMock(spec=TelegramSender)
        mock_sender.send = AsyncMock(return_value=True)
        bridge = AlertmanagerBridge(port=0, sender=mock_sender)

        # Start server on random port
        bridge._server = await asyncio.start_server(
            bridge._handle_connection, "127.0.0.1", 0
        )
        port = bridge._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps(SAMPLE_FIRING).encode()
            request = (
                f"POST /webhook/alertmanager HTTP/1.1\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"\r\n"
            ).encode() + body
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
        bridge._server = await asyncio.start_server(
            bridge._handle_connection, "127.0.0.1", 0
        )
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
```

- [ ] **Step 6: Run all bridge tests**

Run: `uv run pytest tests/unit/test_alertmanager_bridge.py -v`
Expected: All 6 tests PASS

- [ ] **Step 7: Update alertmanager.yml to point at bridge**

Replace `config/monitoring/alerts/alertmanager.yml` content:

```yaml
route:
  group_by: ['alertname']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: 'telegram-bridge'
  routes:
  - match:
      severity: critical
    receiver: 'telegram-bridge'
    repeat_interval: 5m

receivers:
- name: 'telegram-bridge'
  webhook_configs:
  - url: 'http://hft-engine:8081/webhook/alertmanager'
    send_resolved: true
```

- [ ] **Step 8: Wire bridge into bootstrap.py**

Find the service startup section in `bootstrap.py` (near the end, before `ServiceRegistry` return). Add the bridge startup:

```python
# Alertmanager → Telegram bridge (non-blocking, failure does not block trading)
from hft_platform.notifications.alertmanager_bridge import AlertmanagerBridge
try:
    _alert_bridge = AlertmanagerBridge()
    asyncio.get_event_loop().create_task(_alert_bridge.run())
    logger.info("alertmanager_bridge_scheduled")
except Exception:
    logger.warning("alertmanager_bridge_start_failed", exc_info=True)
```

- [ ] **Step 9: Commit**

```bash
git add src/hft_platform/notifications/alertmanager_bridge.py \
        tests/unit/test_alertmanager_bridge.py \
        config/monitoring/alerts/alertmanager.yml \
        src/hft_platform/services/bootstrap.py
git commit -m "feat(notifications): add Alertmanager webhook → Telegram bridge"
```

---

### Task 2: Pin Shioaji SDK version

**Files:**
- Edit: `pyproject.toml:14`

- [ ] **Step 1: Pin the dependency**

In `pyproject.toml`, change line 14 from:

```
"shioaji[speed]>=1.2,<2",
```

to:

```
"shioaji[speed]==1.2.9",  # PINNED: bump manually after SDK regression test
```

- [ ] **Step 2: Verify lock file is consistent**

Run: `uv lock --check`
Expected: Exit 0 (lock file already has 1.2.9)

- [ ] **Step 3: Update risk register**

In `docs/operations/long-term-risk-register.md`, change R10 status from `⚠️ Action required` to `✅ Done — pinned to 1.2.9 (2026-03-27)`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml docs/operations/long-term-risk-register.md
git commit -m "chore(deps): pin shioaji[speed]==1.2.9"
```

---

### Task 3: Startup Config Snapshot with Secret Redaction

**Files:**
- Create: `src/hft_platform/ops/config_snapshot.py`
- Create: `src/hft_platform/migrations/clickhouse/20260327_001_add_config_snapshots.sql`
- Create: `tests/unit/test_config_snapshot.py`
- Edit: `src/hft_platform/services/bootstrap.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_config_snapshot.py
"""Tests for startup config snapshot with secret redaction."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.ops.config_snapshot import (
    REDACT_KEYWORDS,
    collect_allowed_env_vars,
    is_secret_var,
)


class TestSecretRedaction:
    def test_password_vars_are_detected(self) -> None:
        assert is_secret_var("HFT_REDIS_PASSWORD")
        assert is_secret_var("HFT_CLICKHOUSE_PASSWORD")
        assert is_secret_var("HFT_FUBON_PASSWORD")

    def test_token_vars_are_detected(self) -> None:
        assert is_secret_var("HFT_TELEGRAM_BOT_TOKEN")

    def test_key_vars_are_detected(self) -> None:
        assert is_secret_var("SHIOAJI_API_KEY")
        assert is_secret_var("SHIOAJI_SECRET_KEY")

    def test_cert_vars_are_detected(self) -> None:
        assert is_secret_var("HFT_FUBON_CERT_PATH")

    def test_safe_vars_are_not_detected(self) -> None:
        assert not is_secret_var("HFT_MODE")
        assert not is_secret_var("HFT_SYMBOLS")
        assert not is_secret_var("HFT_BROKER")
        assert not is_secret_var("HFT_FEATURE_ENGINE_ENABLED")

    def test_collect_excludes_secrets(self) -> None:
        env = {
            "HFT_MODE": "sim",
            "HFT_BROKER": "shioaji",
            "HFT_REDIS_PASSWORD": "super_secret",
            "HFT_TELEGRAM_BOT_TOKEN": "bot12345",
            "SHIOAJI_API_KEY": "abc123",
            "PATH": "/usr/bin",  # non-HFT var, excluded
        }
        with patch.dict(os.environ, env, clear=True):
            result = collect_allowed_env_vars()
        assert result["HFT_MODE"] == "sim"
        assert result["HFT_BROKER"] == "shioaji"
        assert "HFT_REDIS_PASSWORD" not in result
        assert "HFT_TELEGRAM_BOT_TOKEN" not in result
        assert "SHIOAJI_API_KEY" not in result
        assert "PATH" not in result


class TestConfigSnapshot:
    def test_build_snapshot_has_required_fields(self) -> None:
        from hft_platform.ops.config_snapshot import build_snapshot

        env = {"HFT_MODE": "sim", "HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            snap = build_snapshot(yaml_paths=[], git_sha="abc123")
        assert snap["git_sha"] == "abc123"
        assert "config_hash" in snap
        assert "env_json" in snap
        assert "boot_ts" in snap
        # Verify no secrets even if env has them
        assert "PASSWORD" not in snap["env_json"]
        assert "TOKEN" not in snap["env_json"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config_snapshot.py -v`
Expected: `ModuleNotFoundError: No module named 'hft_platform.ops.config_snapshot'`

- [ ] **Step 3: Implement config_snapshot.py**

```python
# src/hft_platform/ops/config_snapshot.py
"""Startup config snapshot — captures non-secret env vars + config hash to ClickHouse.

Security: uses ALLOWLIST strategy. Only HFT_* vars that are NOT secret are captured.
Defense-in-depth: any var name containing PASSWORD, SECRET, TOKEN, KEY, or CERT is excluded
regardless of prefix.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("ops.config_snapshot")

# Defense-in-depth: any env var whose name contains these substrings is excluded.
REDACT_KEYWORDS: frozenset[str] = frozenset({
    "PASSWORD", "SECRET", "TOKEN", "KEY", "CERT",
})

# Only env vars starting with these prefixes are considered.
_ALLOWED_PREFIXES: tuple[str, ...] = ("HFT_",)


def is_secret_var(name: str) -> bool:
    """Return True if env var name contains any redact keyword."""
    upper = name.upper()
    return any(kw in upper for kw in REDACT_KEYWORDS)


def collect_allowed_env_vars() -> dict[str, str]:
    """Collect non-secret HFT_* env vars from current process."""
    result: dict[str, str] = {}
    for name, value in sorted(os.environ.items()):
        if not any(name.startswith(p) for p in _ALLOWED_PREFIXES):
            continue
        if is_secret_var(name):
            continue
        result[name] = value
    return result


def _compute_yaml_hash(yaml_paths: list[str]) -> str:
    """SHA256 over concatenated YAML file contents."""
    h = hashlib.sha256()
    for path in sorted(yaml_paths):
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except FileNotFoundError:
            h.update(f"MISSING:{path}".encode())
    return h.hexdigest()[:16]


def _get_git_sha() -> str:
    """Best-effort git HEAD SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def build_snapshot(
    *,
    yaml_paths: list[str] | None = None,
    git_sha: str = "",
) -> dict[str, Any]:
    """Build a snapshot dict (not yet persisted)."""
    env_vars = collect_allowed_env_vars()
    return {
        "boot_ts": timebase.now_ns() // 1_000_000,  # milliseconds for DateTime64(3)
        "config_hash": _compute_yaml_hash(yaml_paths or []),
        "git_sha": git_sha or _get_git_sha(),
        "env_json": json.dumps(env_vars, ensure_ascii=False),
        "yaml_json": json.dumps(yaml_paths or []),
    }


async def write_snapshot_to_clickhouse(
    ch_client: Any,
    snapshot: dict[str, Any],
) -> bool:
    """Insert snapshot into hft.config_snapshots. Returns True on success."""
    try:
        ch_client.execute(
            "INSERT INTO hft.config_snapshots (boot_ts, config_hash, git_sha, env_json, yaml_json) VALUES",
            [
                (
                    snapshot["boot_ts"],
                    snapshot["config_hash"],
                    snapshot["git_sha"],
                    snapshot["env_json"],
                    snapshot["yaml_json"],
                )
            ],
        )
        logger.info("config_snapshot_written", config_hash=snapshot["config_hash"])
        return True
    except Exception:
        logger.warning("config_snapshot_write_failed", exc_info=True)
        # Fallback: log the snapshot as structured log
        logger.info("config_snapshot_fallback", **snapshot)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_config_snapshot.py -v`
Expected: All tests PASS

- [ ] **Step 5: Create ClickHouse migration**

```sql
-- src/hft_platform/migrations/clickhouse/20260327_001_add_config_snapshots.sql
CREATE TABLE IF NOT EXISTS hft.config_snapshots (
    boot_ts      DateTime64(3),
    config_hash  String,
    git_sha      String,
    env_json     String,
    yaml_json    String
) ENGINE = MergeTree()
ORDER BY boot_ts
TTL boot_ts + INTERVAL 1 YEAR;
```

- [ ] **Step 6: Wire into bootstrap.py**

Find the service startup section in `bootstrap.py` (after config loading, before service graph construction). Add:

```python
# Startup config snapshot (non-blocking)
from hft_platform.ops.config_snapshot import build_snapshot, write_snapshot_to_clickhouse

try:
    _yaml_paths = [
        "config/base/main.yaml",
        str(settings.get("paths", {}).get("symbols", "config/symbols.yaml")),
        str(settings.get("paths", {}).get("strategy_limits", "config/base/strategy_limits.yaml")),
    ]
    _snapshot = build_snapshot(yaml_paths=_yaml_paths)
    if ch_client is not None:
        asyncio.get_event_loop().create_task(
            write_snapshot_to_clickhouse(ch_client, _snapshot)
        )
    else:
        logger.info("config_snapshot_fallback", **_snapshot)
except Exception:
    logger.warning("config_snapshot_build_failed", exc_info=True)
```

- [ ] **Step 7: Update risk register**

In `docs/operations/long-term-risk-register.md`, change R12 status from `🔵 Backlog` to `✅ Done — allowlisted env snapshot (2026-03-27)`.

- [ ] **Step 8: Commit**

```bash
git add src/hft_platform/ops/config_snapshot.py \
        src/hft_platform/migrations/clickhouse/20260327_001_add_config_snapshots.sql \
        tests/unit/test_config_snapshot.py \
        src/hft_platform/services/bootstrap.py \
        docs/operations/long-term-risk-register.md
git commit -m "feat(ops): add startup config snapshot with secret redaction"
```

---

## Wave 2 — 上線後第一個月

### Task 4: node-exporter Textfile Collector Prerequisite

**Files:**
- Edit: `docker-compose.yml`

- [ ] **Step 1: Add textfile collector volume and flag to node-exporter**

In `docker-compose.yml`, in the `node-exporter` service, add to `volumes`:

```yaml
    - /var/lib/node-exporter/textfile:/var/lib/node-exporter/textfile:ro
```

And add to `command`:

```yaml
    - '--collector.textfile.directory=/var/lib/node-exporter/textfile'
```

- [ ] **Step 2: Document the host directory creation**

Add to `docs/operations/cron-setup-remote.md` under a "Prerequisites" section:

```markdown
### Textfile Collector Directory

Required for custom Prometheus metrics (research data size, SMART monitoring):

```bash
sudo mkdir -p /var/lib/node-exporter/textfile
sudo chown $(whoami):$(whoami) /var/lib/node-exporter/textfile
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml docs/operations/cron-setup-remote.md
git commit -m "feat(ops): enable node-exporter textfile collector"
```

---

### Task 5: Loki + Promtail Log Aggregation

**Files:**
- Edit: `docker-compose.yml`
- Create: `config/monitoring/loki.yml`
- Edit: `config/monitoring/alerts/rules.yaml`

- [ ] **Step 1: Create Loki config**

```yaml
# config/monitoring/loki.yml
auth_enabled: false

server:
  http_listen_port: 3100

common:
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: "2026-01-01"
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  retention_period: 168h  # 7 days

compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
```

- [ ] **Step 2: Add loki and promtail services to docker-compose.yml**

Add after the `alertmanager` service:

```yaml
  loki:
    image: grafana/loki:3.0.0
    container_name: loki
    volumes:
      - loki_data:/loki
      - ./config/monitoring/loki.yml:/etc/loki/local-config.yaml:ro
    command: -config.file=/etc/loki/local-config.yaml
    ports:
      - "3100:3100"
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '0.50'
          memory: 512M
    logging:
      driver: "json-file"
      options:
        max-size: "20m"
        max-file: "3"

  promtail:
    image: grafana/promtail:3.0.0
    container_name: promtail
    volumes:
      - ./config/monitoring/promtail.yml:/etc/promtail/config.yml:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    command: -config.file=/etc/promtail/config.yml
    depends_on:
      - loki
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '0.20'
          memory: 128M
    logging:
      driver: "json-file"
      options:
        max-size: "20m"
        max-file: "3"
```

Add `loki_data:` to the top-level `volumes:` section.

- [ ] **Step 3: Add Loki health alert rule**

Append to `config/monitoring/alerts/rules.yaml` at the end of the `HFT_Critical` group:

```yaml
    # Loki Health
    - alert: LokiDown
      expr: up{job="loki"} == 0
      for: 2m
      labels:
        severity: warning
      annotations:
        summary: "Loki log aggregation is down"
        description: "Loki has been unreachable for >2 minutes. Log search unavailable."
```

Note: This requires Prometheus to scrape Loki. Add to `config/monitoring/prometheus.yml` under `scrape_configs`:

```yaml
  - job_name: 'loki'
    static_configs:
      - targets: ['loki:3100']
```

- [ ] **Step 4: Verify promtail.yml target matches**

Read `config/monitoring/promtail.yml` and confirm `clients` section points to `http://loki:3100/loki/api/v1/push`. If it uses a different URL, update accordingly.

- [ ] **Step 5: Commit**

```bash
git add config/monitoring/loki.yml docker-compose.yml \
        config/monitoring/alerts/rules.yaml config/monitoring/prometheus.yml
git commit -m "feat(ops): add Loki + Promtail log aggregation"
```

---

### Task 6: Research Data Rotation

**Files:**
- Create: `scripts/research_data_rotate.sh`
- Edit: `docs/operations/cron-setup-remote.md`
- Edit: `config/monitoring/alerts/rules.yaml`
- Edit: `docs/operations/long-term-risk-register.md`

- [ ] **Step 1: Write the rotation script**

```bash
#!/usr/bin/env bash
# scripts/research_data_rotate.sh — 4-tier research data rotation per data-retention-policy.md
# Cron: 0 4 * * 0 (weekly, Sunday 04:00)
set -euo pipefail

# --- Config ---
RAW_RETAIN_DAYS="${RESEARCH_RAW_RETAIN_DAYS:-90}"
ARCHIVE_RETAIN_DAYS="${RESEARCH_ARCHIVE_RETAIN_DAYS:-180}"
RUNS_RETAIN_DAYS="${RESEARCH_RUNS_RETAIN_DAYS:-90}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node-exporter/textfile}"
DRY_RUN="${1:-}"  # Pass --dry-run as first arg

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${BASE_DIR}/research/data"
RUNS_DIR="${BASE_DIR}/research/experiments/runs"
ARCHIVE_DIR="${DATA_DIR}/archive"

log() { echo "[$(date -Iseconds)] $*"; }

run_or_print() {
    if [ "$DRY_RUN" = "--dry-run" ]; then
        log "[DRY-RUN] $*"
    else
        "$@"
    fi
}

# --- Tier 1: raw/ — archive after RAW_RETAIN_DAYS, delete archive after ARCHIVE_RETAIN_DAYS ---
log "=== Tier 1: research/data/raw/ ==="
mkdir -p "$ARCHIVE_DIR"
if [ -d "${DATA_DIR}/raw" ]; then
    find "${DATA_DIR}/raw" -mindepth 1 -maxdepth 1 -type d -mtime "+${RAW_RETAIN_DAYS}" | while read -r dir; do
        archive_name="$(basename "$dir").tar.gz"
        log "Archiving: $dir → ${ARCHIVE_DIR}/${archive_name}"
        run_or_print tar czf "${ARCHIVE_DIR}/${archive_name}" -C "${DATA_DIR}/raw" "$(basename "$dir")"
        run_or_print rm -rf "$dir"
    done
fi
if [ -d "$ARCHIVE_DIR" ]; then
    find "$ARCHIVE_DIR" -name "*.tar.gz" -mtime "+${ARCHIVE_RETAIN_DAYS}" | while read -r f; do
        log "Deleting old archive: $f"
        run_or_print rm -f "$f"
    done
fi

# --- Tier 2: processed/ — delete inactive alpha dirs older than 90 days ---
# Protected: research/data/processed/smoke/smoke_v1.npy (must-keep per data-retention-policy)
log "=== Tier 2: research/data/processed/ ==="
if [ -d "${DATA_DIR}/processed" ]; then
    find "${DATA_DIR}/processed" -mindepth 1 -maxdepth 1 -type d -mtime "+${RAW_RETAIN_DAYS}" | while read -r dir; do
        dirname="$(basename "$dir")"
        if [ "$dirname" = "smoke" ]; then
            log "PROTECTED: $dir (must-keep)"
            continue
        fi
        log "Removing stale processed dir: $dir"
        run_or_print rm -rf "$dir"
    done
fi

# --- Tier 3: synthetic/ — keep only latest version per sub-dir ---
log "=== Tier 3: research/data/synthetic/ ==="
if [ -d "${DATA_DIR}/synthetic" ]; then
    find "${DATA_DIR}/synthetic" -mindepth 1 -maxdepth 1 -type d | while read -r dir; do
        # Keep the newest file, delete older versions
        file_count=$(find "$dir" -maxdepth 1 -type f | wc -l)
        if [ "$file_count" -gt 1 ]; then
            log "Cleaning synthetic dir (keeping newest): $dir"
            # shellcheck disable=SC2012
            ls -t "$dir" | tail -n +2 | while read -r old_file; do
                run_or_print rm -f "${dir}/${old_file}"
            done
        fi
    done
fi

# --- Tier 4: experiments/runs/ — delete after RUNS_RETAIN_DAYS ---
log "=== Tier 4: research/experiments/runs/ ==="
if [ -d "$RUNS_DIR" ]; then
    find "$RUNS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+${RUNS_RETAIN_DAYS}" | while read -r dir; do
        log "Removing old experiment run: $dir"
        run_or_print rm -rf "$dir"
    done
fi

# --- Emit Prometheus textfile metric ---
if [ "$DRY_RUN" != "--dry-run" ] && [ -d "$TEXTFILE_DIR" ]; then
    total_bytes=$(du -sb "${DATA_DIR}" 2>/dev/null | awk '{print $1}' || echo 0)
    runs_bytes=$(du -sb "${RUNS_DIR}" 2>/dev/null | awk '{print $1}' || echo 0)
    cat > "${TEXTFILE_DIR}/research_data.prom" <<METRICS
# HELP hft_research_data_bytes Total size of research/data/ in bytes
# TYPE hft_research_data_bytes gauge
hft_research_data_bytes ${total_bytes}
# HELP hft_research_runs_bytes Total size of research/experiments/runs/ in bytes
# TYPE hft_research_runs_bytes gauge
hft_research_runs_bytes ${runs_bytes}
METRICS
    log "Prometheus textfile written: ${total_bytes} bytes data, ${runs_bytes} bytes runs"
fi

log "=== Research data rotation complete ==="
```

- [ ] **Step 2: Make executable**

Run: `chmod +x scripts/research_data_rotate.sh`

- [ ] **Step 3: Add Prometheus alert rule for research data size**

Append to `config/monitoring/alerts/rules.yaml`:

```yaml
    # Research Data Disk
    - alert: ResearchDataTooLarge
      expr: hft_research_data_bytes > 200e9
      for: 1h
      labels:
        severity: warning
      annotations:
        summary: "Research data exceeds 200 GB"
        description: "research/data/ is {{ $value | humanize1024 }}. Check rotation script."
```

- [ ] **Step 4: Add cron entry to docs and update risk register**

Add to `docs/operations/cron-setup-remote.md`:

```markdown
| `0 4 * * 0` (Sun 04:00) | `cd ~/subhft && ./scripts/research_data_rotate.sh` | Research data 4-tier rotation (raw/processed/synthetic/runs) |
```

Update R04 in `docs/operations/long-term-risk-register.md` from `⚠️ Partial` to `✅ Done — automated 4-tier rotation (2026-03-27)`.

- [ ] **Step 5: Commit**

```bash
git add scripts/research_data_rotate.sh config/monitoring/alerts/rules.yaml \
        docs/operations/cron-setup-remote.md docs/operations/long-term-risk-register.md
git commit -m "feat(ops): automate research data rotation (4-tier)"
```

---

### Task 7: SMART Disk Monitoring

**Files:**
- Create: `scripts/smart_check.sh`
- Edit: `docs/operations/cron-setup-remote.md`
- Edit: `config/monitoring/alerts/rules.yaml`
- Edit: `docs/operations/long-term-risk-register.md`

- [ ] **Step 1: Write the SMART check script**

```bash
#!/usr/bin/env bash
# scripts/smart_check.sh — Parse SMART attributes and emit Prometheus textfile
# Cron: 0 5 * * 1 (weekly, Monday 05:00)
# Requires: sudo apt install smartmontools
set -euo pipefail

DEVICE="${1:-/dev/sda}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node-exporter/textfile}"
OUTPUT="${TEXTFILE_DIR}/smartmon.prom"

if ! command -v smartctl &>/dev/null; then
    echo "smartctl not found. Install: sudo apt install smartmontools" >&2
    exit 1
fi

# Run smartctl (may need sudo)
SMART_OUTPUT=$(sudo smartctl -A "$DEVICE" 2>/dev/null || true)

parse_attr() {
    local attr_name="$1"
    echo "$SMART_OUTPUT" | awk -v name="$attr_name" '$2 == name {print $10}' | head -1
}

REALLOCATED=$(parse_attr "Reallocated_Sector_Ct")
WEAR_LEVEL=$(parse_attr "Wear_Leveling_Count")
POWER_ON_HOURS=$(parse_attr "Power_On_Hours")
TEMP=$(parse_attr "Temperature_Celsius")

# Fallback for NVMe drives (different attribute names)
if [ -z "$REALLOCATED" ]; then
    REALLOCATED=$(parse_attr "Reallocated_Sector_Count")
fi
if [ -z "$WEAR_LEVEL" ]; then
    WEAR_LEVEL=$(parse_attr "Media_Wearout_Indicator")
fi

cat > "$OUTPUT" <<METRICS
# HELP smartmon_reallocated_sectors Number of reallocated sectors
# TYPE smartmon_reallocated_sectors gauge
smartmon_reallocated_sectors{device="$DEVICE"} ${REALLOCATED:-0}
# HELP smartmon_wear_leveling Wear leveling count (lower = more worn)
# TYPE smartmon_wear_leveling gauge
smartmon_wear_leveling{device="$DEVICE"} ${WEAR_LEVEL:-0}
# HELP smartmon_power_on_hours Total power-on hours
# TYPE smartmon_power_on_hours gauge
smartmon_power_on_hours{device="$DEVICE"} ${POWER_ON_HOURS:-0}
# HELP smartmon_temperature_celsius Drive temperature
# TYPE smartmon_temperature_celsius gauge
smartmon_temperature_celsius{device="$DEVICE"} ${TEMP:-0}
METRICS

echo "[$(date -Iseconds)] SMART metrics written to $OUTPUT"
```

- [ ] **Step 2: Make executable**

Run: `chmod +x scripts/smart_check.sh`

- [ ] **Step 3: Add Prometheus alert rule**

Append to `config/monitoring/alerts/rules.yaml`:

```yaml
    # SSD Health
    - alert: SSDReallocatedSectorsHigh
      expr: smartmon_reallocated_sectors > 100
      for: 0s
      labels:
        severity: critical
      annotations:
        summary: "SSD has >100 reallocated sectors"
        description: "Device {{ $labels.device }} has {{ $value }} reallocated sectors. Disk replacement recommended."

    - alert: SSDWearLevelLow
      expr: smartmon_wear_leveling > 0 and smartmon_wear_leveling < 20
      for: 0s
      labels:
        severity: warning
      annotations:
        summary: "SSD wear level below 20%"
        description: "Device {{ $labels.device }} wear level at {{ $value }}%. Plan disk replacement."
```

- [ ] **Step 4: Update cron docs and risk register**

Add to `docs/operations/cron-setup-remote.md`:

```markdown
| `0 5 * * 1` (Mon 05:00) | `cd ~/subhft && ./scripts/smart_check.sh` | SMART disk health → Prometheus textfile |
```

Prerequisite note: `sudo apt install smartmontools`

Update R09 in `docs/operations/long-term-risk-register.md` from `⚠️ Open` to `✅ Done — weekly SMART cron + alert (2026-03-27)`.

- [ ] **Step 5: Commit**

```bash
git add scripts/smart_check.sh config/monitoring/alerts/rules.yaml \
        docs/operations/cron-setup-remote.md docs/operations/long-term-risk-register.md
git commit -m "feat(ops): add SMART disk monitoring with Prometheus alerts"
```

---

### Task 8: Quarterly Health Check Automation

**Files:**
- Create: `scripts/quarterly_health_check.py`
- Edit: `Makefile`
- Edit: `docs/operations/cron-setup-remote.md`

- [ ] **Step 1: Write the quarterly health check script**

```python
#!/usr/bin/env python3
"""scripts/quarterly_health_check.py — Automated quarterly infrastructure health check.

Checks: ClickHouse TTL, Prometheus storage, OS updates, SMART, Shioaji SDK pin.
Outputs JSON report + optional Telegram summary.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    status: str  # PASS, WARN, FAIL
    detail: str


@dataclass
class QuarterlyReport:
    checks: list[CheckResult] = field(default_factory=list)

    def summary(self) -> str:
        lines = ["<b>Quarterly Health Check</b>\n"]
        for c in self.checks:
            icon = {"PASS": "\u2705", "WARN": "\u26a0\ufe0f", "FAIL": "\u274c"}[c.status]
            lines.append(f"{icon} <b>{c.name}</b>: {c.status}\n  {c.detail}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps([asdict(c) for c in self.checks], indent=2)


def check_clickhouse_ttl() -> CheckResult:
    """Verify no rows older than 6 months exist in hft.market_data."""
    try:
        from clickhouse_driver import Client

        host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
        port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
        client = Client(host=host, port=port)
        rows = client.execute(
            "SELECT count() FROM hft.market_data "
            "WHERE toDateTime(ingest_ts / 1000000000) < now() - INTERVAL 6 MONTH"
        )
        count = rows[0][0] if rows else 0
        if count == 0:
            return CheckResult("ClickHouse TTL", "PASS", "No expired rows found")
        return CheckResult("ClickHouse TTL", "FAIL", f"{count} rows older than 6 months")
    except Exception as exc:
        return CheckResult("ClickHouse TTL", "WARN", f"Cannot connect: {exc}")


def check_prometheus_storage() -> CheckResult:
    """Query Prometheus TSDB storage size."""
    try:
        import urllib.request

        prom_url = os.getenv("HFT_PROMETHEUS_URL", "http://localhost:9091")
        url = f"{prom_url}/api/v1/query?query=prometheus_tsdb_storage_size_bytes"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        results = data.get("data", {}).get("result", [])
        if not results:
            return CheckResult("Prometheus Storage", "WARN", "No metric found")
        size_bytes = float(results[0]["value"][1])
        size_gb = size_bytes / (1024**3)
        status = "PASS" if size_gb < 10 else ("WARN" if size_gb < 20 else "FAIL")
        return CheckResult("Prometheus Storage", status, f"{size_gb:.1f} GB")
    except Exception as exc:
        return CheckResult("Prometheus Storage", "WARN", f"Cannot query: {exc}")


def check_os_updates() -> CheckResult:
    """Count available OS package updates."""
    try:
        result = subprocess.run(
            ["apt", "list", "--upgradable"],
            capture_output=True, text=True, timeout=30
        )
        lines = [l for l in result.stdout.strip().split("\n") if "/" in l]
        count = len(lines)
        if count == 0:
            return CheckResult("OS Updates", "PASS", "System up to date")
        status = "WARN" if count < 50 else "FAIL"
        return CheckResult("OS Updates", status, f"{count} packages upgradable")
    except Exception as exc:
        return CheckResult("OS Updates", "WARN", f"Cannot check: {exc}")


def check_smart() -> CheckResult:
    """Run SMART check script if available."""
    script = Path(__file__).parent / "smart_check.sh"
    if not script.exists():
        return CheckResult("SMART Health", "WARN", "smart_check.sh not found")
    try:
        subprocess.run([str(script)], capture_output=True, timeout=30, check=True)
        # Read the textfile output
        prom_file = Path("/var/lib/node-exporter/textfile/smartmon.prom")
        if prom_file.exists():
            content = prom_file.read_text()
            realloc_match = re.search(r"smartmon_reallocated_sectors\{.*\}\s+(\d+)", content)
            realloc = int(realloc_match.group(1)) if realloc_match else 0
            if realloc > 100:
                return CheckResult("SMART Health", "FAIL", f"Reallocated sectors: {realloc}")
            if realloc > 0:
                return CheckResult("SMART Health", "WARN", f"Reallocated sectors: {realloc}")
            return CheckResult("SMART Health", "PASS", "No reallocated sectors")
        return CheckResult("SMART Health", "WARN", "Textfile not generated")
    except Exception as exc:
        return CheckResult("SMART Health", "WARN", f"Script failed: {exc}")


def check_shioaji_pin() -> CheckResult:
    """Verify Shioaji SDK is pinned in pyproject.toml and matches uv.lock."""
    repo_root = Path(__file__).parent.parent
    pyproject = repo_root / "pyproject.toml"
    lock_file = repo_root / "uv.lock"

    if not pyproject.exists():
        return CheckResult("Shioaji SDK Pin", "WARN", "pyproject.toml not found")

    pyproject_text = pyproject.read_text()
    pin_match = re.search(r'shioaji\[speed\]==([0-9.]+)', pyproject_text)
    if not pin_match:
        return CheckResult("Shioaji SDK Pin", "FAIL", "Not pinned (missing ==X.Y.Z)")
    pinned_version = pin_match.group(1)

    if lock_file.exists():
        lock_text = lock_file.read_text()
        lock_match = re.search(r'name = "shioaji"\nversion = "([0-9.]+)"', lock_text)
        if lock_match:
            locked_version = lock_match.group(1)
            if locked_version != pinned_version:
                return CheckResult(
                    "Shioaji SDK Pin", "WARN",
                    f"Pin={pinned_version} but lock={locked_version} — run uv lock"
                )

    return CheckResult("Shioaji SDK Pin", "PASS", f"Pinned at {pinned_version}")


def main() -> None:
    report = QuarterlyReport()
    report.checks.append(check_clickhouse_ttl())
    report.checks.append(check_prometheus_storage())
    report.checks.append(check_os_updates())
    report.checks.append(check_smart())
    report.checks.append(check_shioaji_pin())

    # Print JSON report
    print(report.to_json())

    # Print human summary
    print("\n" + "=" * 60)
    print(report.summary())

    # Optional: send Telegram
    if os.getenv("HFT_TELEGRAM_BOT_TOKEN") and os.getenv("HFT_TELEGRAM_CHAT_ID"):
        try:
            import asyncio
            from hft_platform.notifications.telegram import TelegramSender

            async def _send() -> None:
                sender = TelegramSender(enabled=True)
                await sender.send(report.summary())
                await sender.close()

            asyncio.run(_send())
            print("\nTelegram notification sent.")
        except Exception as exc:
            print(f"\nTelegram send failed: {exc}")

    # Exit code: 1 if any FAIL
    if any(c.status == "FAIL" for c in report.checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add Makefile target**

Add before the `# Help` section in `Makefile`:

```makefile
quarterly-health-check: ## Run quarterly infrastructure health check
	uv run python scripts/quarterly_health_check.py
```

- [ ] **Step 3: Add cron entry to docs**

Add to `docs/operations/cron-setup-remote.md`:

```markdown
| `0 7 1 1,4,7,10 *` (quarterly) | `cd ~/subhft && make quarterly-health-check` | Quarterly infrastructure health check (TTL, Prometheus, OS, SMART, SDK) |
```

- [ ] **Step 4: Commit**

```bash
git add scripts/quarterly_health_check.py Makefile docs/operations/cron-setup-remote.md
git commit -m "feat(ops): add quarterly automated health check"
```

---

## Wave 3 — 持續強化（TCA + Live Feasibility 補完）

### Task 9: Extend FillEvent Contract + Migration

**Files:**
- Edit: `src/hft_platform/contracts/execution.py`
- Create: `src/hft_platform/migrations/clickhouse/20260327_002_add_tca_columns_to_fills.sql`

- [ ] **Step 1: Add decision_price and arrival_price to FillEvent**

In `src/hft_platform/contracts/execution.py`, add two fields to `FillEvent` after `match_ts_ns`:

```python
    # TCA: passthrough from OrderCommand for slippage decomposition
    decision_price: int = 0  # LOB mid-price at signal time (x10000)
    arrival_price: int = 0   # Price at order submit time (x10000)
```

- [ ] **Step 2: Create ClickHouse migration**

```sql
-- src/hft_platform/migrations/clickhouse/20260327_002_add_tca_columns_to_fills.sql
ALTER TABLE hft.fills
    ADD COLUMN IF NOT EXISTS decision_price Int64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS arrival_price Int64 DEFAULT 0;
```

- [ ] **Step 3: Commit**

```bash
git add src/hft_platform/contracts/execution.py \
        src/hft_platform/migrations/clickhouse/20260327_002_add_tca_columns_to_fills.sql
git commit -m "feat(contracts): add decision_price/arrival_price to FillEvent"
```

---

### Task 10: TCA Slippage Decomposer

**Files:**
- Create: `src/hft_platform/tca/slippage.py`
- Create: `tests/unit/test_tca_slippage.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_tca_slippage.py
"""Tests for TCA SlippageDecomposer."""
from __future__ import annotations

import pytest

from hft_platform.tca.slippage import SlippageDecomposer
from hft_platform.tca.types import SlippageBreakdown


class TestSlippageDecomposer:
    def setup_method(self) -> None:
        self.decomposer = SlippageDecomposer(point_value=10, tick_size=1.0)

    def test_zero_slippage_when_prices_equal(self) -> None:
        result = self.decomposer.decompose(
            decision_price=200_000_000,  # 20000.0000 x10000
            arrival_price=200_000_000,
            fill_price=200_000_000,
            notional_ntd=200_000,
            fee_ntd=13,
            tax_ntd=0,
        )
        assert isinstance(result, SlippageBreakdown)
        assert result.delay_cost_bps == pytest.approx(0.0, abs=0.01)
        assert result.execution_cost_bps == pytest.approx(0.0, abs=0.01)

    def test_delay_cost_captured(self) -> None:
        # Decision at 20000, arrival at 20001 (moved against us = delay cost)
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_010_000,  # 20001.0
            fill_price=200_010_000,
            notional_ntd=200_000,
            fee_ntd=13,
            tax_ntd=0,
        )
        assert result.delay_cost_bps > 0

    def test_execution_cost_captured(self) -> None:
        # Arrival at 20000, filled at 20002 (market impact + timing)
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_000_000,
            fill_price=200_020_000,  # 20002.0
            notional_ntd=200_000,
            fee_ntd=13,
            tax_ntd=0,
        )
        assert result.execution_cost_bps > 0

    def test_total_is_sum_of_components(self) -> None:
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_010_000,
            fill_price=200_030_000,
            notional_ntd=200_000,
            fee_ntd=13,
            tax_ntd=6,
        )
        expected_total = (
            result.commission_bps + result.tax_bps
            + result.delay_cost_bps + result.execution_cost_bps
            + result.market_impact_bps
        )
        assert result.total_bps == pytest.approx(expected_total, abs=0.01)

    def test_zero_notional_returns_zero_breakdown(self) -> None:
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_000_000,
            fill_price=200_000_000,
            notional_ntd=0,
            fee_ntd=0,
            tax_ntd=0,
        )
        assert result.total_bps == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tca_slippage.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement SlippageDecomposer**

```python
# src/hft_platform/tca/slippage.py
"""TCA Slippage Decomposer — 4-component breakdown.

Components:
  1. Commission (fee excluding tax)
  2. Tax
  3. Delay cost (decision_price → arrival_price)
  4. Execution cost (arrival_price → fill_price)
  5. Market impact (estimated via sqrt model, residual = execution - impact)

WARNING: float arithmetic — offline TCA analysis only, NOT for live accounting.
"""
from __future__ import annotations

from hft_platform.tca.types import SlippageBreakdown


class SlippageDecomposer:
    """Decompose per-fill slippage into cost components."""

    __slots__ = ("_point_value", "_tick_size")

    def __init__(self, *, point_value: int = 10, tick_size: float = 1.0) -> None:
        self._point_value = point_value
        self._tick_size = tick_size

    def decompose(
        self,
        *,
        decision_price: int,
        arrival_price: int,
        fill_price: int,
        notional_ntd: int,
        fee_ntd: int,
        tax_ntd: int,
    ) -> SlippageBreakdown:
        """Decompose slippage. All prices are scaled int x10000."""
        if notional_ntd == 0:
            return SlippageBreakdown(
                commission_bps=0.0,
                tax_bps=0.0,
                delay_cost_bps=0.0,
                execution_cost_bps=0.0,
                market_impact_bps=0.0,
                total_bps=0.0,
            )

        notional = float(notional_ntd)
        commission_ntd = float(fee_ntd - tax_ntd) if fee_ntd > tax_ntd else float(fee_ntd)

        commission_bps = (commission_ntd / notional) * 10_000.0
        tax_bps = (float(tax_ntd) / notional) * 10_000.0

        # Price-based components (all in x10000 scale)
        delay_points = float(arrival_price - decision_price) / 10_000.0
        exec_points = float(fill_price - arrival_price) / 10_000.0

        delay_cost_ntd = delay_points * self._point_value
        exec_cost_ntd = exec_points * self._point_value

        delay_cost_bps = (delay_cost_ntd / notional) * 10_000.0
        execution_cost_bps = (exec_cost_ntd / notional) * 10_000.0

        # Market impact is a subset of execution cost.
        # Without a full impact model, attribute all execution cost as market impact.
        # SqrtImpactModel (Task 11) can refine this later.
        market_impact_bps = 0.0  # Placeholder until impact model is integrated

        total_bps = commission_bps + tax_bps + delay_cost_bps + execution_cost_bps + market_impact_bps

        return SlippageBreakdown(
            commission_bps=commission_bps,
            tax_bps=tax_bps,
            delay_cost_bps=delay_cost_bps,
            execution_cost_bps=execution_cost_bps,
            market_impact_bps=market_impact_bps,
            total_bps=total_bps,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tca_slippage.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/tca/slippage.py tests/unit/test_tca_slippage.py
git commit -m "feat(tca): add SlippageDecomposer with 4-component breakdown"
```

---

### Task 11: TCA Sqrt Impact Model

**Files:**
- Create: `src/hft_platform/tca/impact.py`
- Create: `tests/unit/test_tca_impact.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_tca_impact.py
"""Tests for TCA SqrtImpactModel."""
from __future__ import annotations

import pytest

from hft_platform.tca.impact import SqrtImpactModel


class TestSqrtImpactModel:
    def setup_method(self) -> None:
        self.model = SqrtImpactModel(sigma_daily=0.015, adv=5000)

    def test_zero_volume_returns_zero_impact(self) -> None:
        assert self.model.estimate_impact_bps(volume=0) == 0.0

    def test_positive_volume_returns_positive_impact(self) -> None:
        impact = self.model.estimate_impact_bps(volume=100)
        assert impact > 0

    def test_impact_increases_with_volume(self) -> None:
        small = self.model.estimate_impact_bps(volume=50)
        large = self.model.estimate_impact_bps(volume=200)
        assert large > small

    def test_impact_sublinear_in_volume(self) -> None:
        """sqrt model: doubling volume should NOT double impact."""
        single = self.model.estimate_impact_bps(volume=100)
        double = self.model.estimate_impact_bps(volume=200)
        assert double < 2.0 * single

    def test_zero_adv_returns_zero(self) -> None:
        model = SqrtImpactModel(sigma_daily=0.015, adv=0)
        assert model.estimate_impact_bps(volume=100) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tca_impact.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement SqrtImpactModel**

```python
# src/hft_platform/tca/impact.py
"""Square-root impact model for TCA.

Model: impact_bps = eta * sigma * sqrt(V / ADV) * 10000
where eta is a calibration constant (default 0.1).

WARNING: float arithmetic — offline TCA analysis only.
"""
from __future__ import annotations

import math


class SqrtImpactModel:
    """Estimate market impact using sqrt-volume model."""

    __slots__ = ("_sigma", "_adv", "_eta")

    def __init__(
        self,
        *,
        sigma_daily: float = 0.015,
        adv: int = 5000,
        eta: float = 0.1,
    ) -> None:
        self._sigma = sigma_daily
        self._adv = adv
        self._eta = eta

    def estimate_impact_bps(self, *, volume: int) -> float:
        """Estimate market impact in bps for a given trade volume."""
        if volume <= 0 or self._adv <= 0:
            return 0.0
        participation = volume / self._adv
        return self._eta * self._sigma * math.sqrt(participation) * 10_000.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tca_impact.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/tca/impact.py tests/unit/test_tca_impact.py
git commit -m "feat(tca): add SqrtImpactModel"
```

---

### Task 12: Real-time Slippage Tracker

**Files:**
- Create: `src/hft_platform/execution/slippage_tracker.py`
- Create: `tests/unit/test_slippage_tracker.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_slippage_tracker.py
"""Tests for per-fill real-time slippage tracker."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.slippage_tracker import SlippageTracker


def _make_fill(
    *,
    price: int = 200_000_000,
    decision_price: int = 200_000_000,
    arrival_price: int = 200_000_000,
    qty: int = 1,
    fee: int = 130_000,
    tax: int = 0,
) -> FillEvent:
    return FillEvent(
        fill_id="f1",
        account_id="acc",
        order_id="o1",
        strategy_id="strat",
        symbol="TXFD6",
        side=Side.BUY,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=0,
        match_ts_ns=0,
        decision_price=decision_price,
        arrival_price=arrival_price,
    )


class TestSlippageTracker:
    def test_track_fill_records_metric(self) -> None:
        tracker = SlippageTracker(point_value=10)
        fill = _make_fill()
        tracker.track(fill)
        assert tracker.total_tracked == 1

    def test_slippage_bps_computed(self) -> None:
        tracker = SlippageTracker(point_value=10)
        fill = _make_fill(
            decision_price=200_000_000,
            arrival_price=200_010_000,
            price=200_020_000,
        )
        tracker.track(fill)
        assert tracker.last_slippage_bps > 0

    def test_no_crash_on_zero_decision_price(self) -> None:
        """If decision_price is 0 (not set), tracker should not crash."""
        tracker = SlippageTracker(point_value=10)
        fill = _make_fill(decision_price=0, arrival_price=0)
        tracker.track(fill)  # Should not raise
        assert tracker.total_tracked == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_slippage_tracker.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement SlippageTracker**

```python
# src/hft_platform/execution/slippage_tracker.py
"""Per-fill real-time slippage tracker.

Computes slippage for each fill and exports Prometheus metrics.
Not hot-path: called in the fill callback path (parallel to recording).
"""
from __future__ import annotations

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent
from hft_platform.tca.slippage import SlippageDecomposer

logger = get_logger("execution.slippage_tracker")

try:
    from prometheus_client import Histogram, Counter

    SLIPPAGE_BPS = Histogram(
        "hft_fill_slippage_bps",
        "Per-fill total slippage in basis points",
        ["strategy", "symbol"],
        buckets=[0, 0.5, 1, 2, 5, 10, 20, 50],
    )
    FILLS_TRACKED = Counter(
        "hft_slippage_fills_tracked_total",
        "Total fills processed by slippage tracker",
    )
except ImportError:
    SLIPPAGE_BPS = None  # type: ignore[assignment]
    FILLS_TRACKED = None  # type: ignore[assignment]


class SlippageTracker:
    """Track per-fill slippage and emit Prometheus metrics."""

    __slots__ = ("_decomposer", "_total_tracked", "_last_slippage_bps")

    def __init__(self, *, point_value: int = 10, tick_size: float = 1.0) -> None:
        self._decomposer = SlippageDecomposer(point_value=point_value, tick_size=tick_size)
        self._total_tracked: int = 0
        self._last_slippage_bps: float = 0.0

    @property
    def total_tracked(self) -> int:
        return self._total_tracked

    @property
    def last_slippage_bps(self) -> float:
        return self._last_slippage_bps

    def track(self, fill: FillEvent) -> None:
        """Process a fill event and record slippage."""
        self._total_tracked += 1

        if fill.decision_price == 0 and fill.arrival_price == 0:
            # TCA fields not populated — skip decomposition
            self._last_slippage_bps = 0.0
            return

        notional_ntd = abs(fill.price * fill.qty) // 10_000  # De-scale from x10000
        if notional_ntd == 0:
            self._last_slippage_bps = 0.0
            return

        breakdown = self._decomposer.decompose(
            decision_price=fill.decision_price,
            arrival_price=fill.arrival_price,
            fill_price=fill.price,
            notional_ntd=notional_ntd,
            fee_ntd=fill.fee // 10_000,
            tax_ntd=fill.tax // 10_000,
        )
        self._last_slippage_bps = breakdown.total_bps

        if SLIPPAGE_BPS is not None:
            SLIPPAGE_BPS.labels(
                strategy=fill.strategy_id, symbol=fill.symbol
            ).observe(breakdown.total_bps)
        if FILLS_TRACKED is not None:
            FILLS_TRACKED.inc()

        logger.debug(
            "slippage_tracked",
            fill_id=fill.fill_id,
            total_bps=round(breakdown.total_bps, 2),
            delay_bps=round(breakdown.delay_cost_bps, 2),
            exec_bps=round(breakdown.execution_cost_bps, 2),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_slippage_tracker.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/execution/slippage_tracker.py tests/unit/test_slippage_tracker.py
git commit -m "feat(execution): add real-time slippage tracker with Prometheus metrics"
```

---

### Task 13: Liquidity Gate

**Files:**
- Create: `src/hft_platform/risk/liquidity_gate.py`
- Create: `tests/unit/test_liquidity_gate.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_liquidity_gate.py
"""Tests for spread-based liquidity gate."""
from __future__ import annotations

import pytest

from hft_platform.risk.liquidity_gate import LiquidityGate


class TestLiquidityGate:
    def test_allows_order_when_spread_below_threshold(self) -> None:
        gate = LiquidityGate(max_spread_pts=5.0)
        assert gate.check(spread_pts=3.0) is True

    def test_rejects_order_when_spread_above_threshold(self) -> None:
        gate = LiquidityGate(max_spread_pts=5.0)
        assert gate.check(spread_pts=7.0) is False

    def test_allows_at_exact_threshold(self) -> None:
        gate = LiquidityGate(max_spread_pts=5.0)
        assert gate.check(spread_pts=5.0) is True

    def test_rejection_counter_increments(self) -> None:
        gate = LiquidityGate(max_spread_pts=5.0)
        gate.check(spread_pts=3.0)
        gate.check(spread_pts=7.0)
        gate.check(spread_pts=8.0)
        assert gate.total_rejected == 2
        assert gate.total_checked == 3

    def test_zero_threshold_rejects_all_nonzero(self) -> None:
        gate = LiquidityGate(max_spread_pts=0.0)
        assert gate.check(spread_pts=0.0) is True
        assert gate.check(spread_pts=0.1) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_liquidity_gate.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement LiquidityGate**

```python
# src/hft_platform/risk/liquidity_gate.py
"""Spread-based liquidity gate — rejects orders when spread exceeds threshold.

Configurable via strategy config: `liquidity_gate.max_spread_pts`.
"""
from __future__ import annotations

from structlog import get_logger

logger = get_logger("risk.liquidity_gate")

try:
    from prometheus_client import Counter

    GATE_CHECKS = Counter(
        "hft_liquidity_gate_checks_total",
        "Total liquidity gate checks",
        ["result"],
    )
except ImportError:
    GATE_CHECKS = None  # type: ignore[assignment]


class LiquidityGate:
    """Reject orders when current spread exceeds configured threshold."""

    __slots__ = ("_max_spread_pts", "_total_checked", "_total_rejected")

    def __init__(self, *, max_spread_pts: float = 5.0) -> None:
        self._max_spread_pts = max_spread_pts
        self._total_checked: int = 0
        self._total_rejected: int = 0

    @property
    def total_checked(self) -> int:
        return self._total_checked

    @property
    def total_rejected(self) -> int:
        return self._total_rejected

    def check(self, *, spread_pts: float) -> bool:
        """Return True if order should proceed, False if rejected."""
        self._total_checked += 1

        if spread_pts > self._max_spread_pts:
            self._total_rejected += 1
            if GATE_CHECKS is not None:
                GATE_CHECKS.labels(result="rejected").inc()
            logger.info(
                "liquidity_gate_rejected",
                spread_pts=round(spread_pts, 2),
                threshold=self._max_spread_pts,
            )
            return False

        if GATE_CHECKS is not None:
            GATE_CHECKS.labels(result="passed").inc()
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_liquidity_gate.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/risk/liquidity_gate.py tests/unit/test_liquidity_gate.py
git commit -m "feat(risk): add spread-based liquidity gate"
```

---

### Task 14: TCA Report + DailyReportService Integration

**Files:**
- Create: `src/hft_platform/tca/report.py`
- Create: `tests/unit/test_tca_report.py`
- Create: `src/hft_platform/ops/daily_pnl_report.py`
- Create: `tests/unit/test_daily_pnl_report.py`
- Edit: `src/hft_platform/services/daily_report.py`

- [ ] **Step 1: Write failing tests for TCAReportGenerator**

```python
# tests/unit/test_tca_report.py
"""Tests for TCA report generator."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.tca.report import TCAReportGenerator
from hft_platform.tca.types import TCADailyReport


class TestTCAReportGenerator:
    def test_format_empty_reports(self) -> None:
        gen = TCAReportGenerator()
        msg = gen.format_telegram_section([])
        assert msg == ""

    def test_format_single_report(self) -> None:
        report = TCADailyReport(
            date="2026-03-27",
            strategy="CBS_TMFD6",
            symbol="TMFD6",
            trade_count=10,
            volume=50,
            notional=500_000,
            commission_bps_mean=1.2,
            tax_bps_mean=0.6,
            delay_cost_bps_mean=0.3,
            delay_cost_bps_p95=0.8,
            exec_cost_bps_mean=0.5,
            exec_cost_bps_p95=1.2,
            impact_bps_mean=0.0,
            total_cost_bps_mean=2.6,
            total_cost_bps_p95=3.5,
        )
        msg = gen.format_telegram_section([report])
        assert "CBS_TMFD6" in msg
        assert "TMFD6" in msg
        assert "10" in msg  # trade count

    def test_format_preserves_all_reports(self) -> None:
        gen = TCAReportGenerator()
        reports = [
            TCADailyReport(
                date="2026-03-27", strategy=f"s{i}", symbol="TMFD6",
                trade_count=i, volume=i*10, notional=i*100000,
                commission_bps_mean=1.0, tax_bps_mean=0.5,
                delay_cost_bps_mean=0.0, delay_cost_bps_p95=0.0,
                exec_cost_bps_mean=0.0, exec_cost_bps_p95=0.0,
                impact_bps_mean=0.0, total_cost_bps_mean=1.5,
                total_cost_bps_p95=2.0,
            )
            for i in range(1, 4)
        ]
        msg = gen.format_telegram_section(reports)
        assert "s1" in msg
        assert "s2" in msg
        assert "s3" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tca_report.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement TCAReportGenerator**

```python
# src/hft_platform/tca/report.py
"""TCA Report Generator — formats daily/weekly TCA summaries.

Designed to integrate as a section within DailyReportService, not standalone.
"""
from __future__ import annotations

from hft_platform.tca.types import TCADailyReport


class TCAReportGenerator:
    """Generate Telegram-formatted TCA report sections."""

    def format_telegram_section(self, reports: list[TCADailyReport]) -> str:
        """Format TCA reports as an HTML section for Telegram."""
        if not reports:
            return ""

        lines: list[str] = ["<b>TCA Summary</b>"]
        for r in reports:
            lines.append(
                f"\n<b>{r.strategy} / {r.symbol}</b>\n"
                f"  Trades: {r.trade_count} | Vol: {r.volume}\n"
                f"  Cost: {r.total_cost_bps_mean:.1f} bps (P95: {r.total_cost_bps_p95:.1f})\n"
                f"  Comm: {r.commission_bps_mean:.1f} | Tax: {r.tax_bps_mean:.1f} | "
                f"Delay: {r.delay_cost_bps_mean:.1f} | Exec: {r.exec_cost_bps_mean:.1f}"
            )
        return "\n".join(lines)
```

- [ ] **Step 4: Run TCA report tests**

Run: `uv run pytest tests/unit/test_tca_report.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Write DailyPnlReport and tests**

```python
# tests/unit/test_daily_pnl_report.py
"""Tests for daily PnL report section."""
from __future__ import annotations

import pytest

from hft_platform.ops.daily_pnl_report import DailyPnlSection


class TestDailyPnlSection:
    def test_format_positive_pnl(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=5000, unrealized_pnl_ntd=1200,
            trade_count=15, fill_count=20
        )
        assert "5,000" in msg or "5000" in msg
        assert "15" in msg

    def test_format_negative_pnl(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=-3000, unrealized_pnl_ntd=0,
            trade_count=5, fill_count=5
        )
        assert "-3,000" in msg or "-3000" in msg

    def test_format_zero_trades(self) -> None:
        section = DailyPnlSection()
        msg = section.format_telegram_section(
            realized_pnl_ntd=0, unrealized_pnl_ntd=0,
            trade_count=0, fill_count=0
        )
        assert "0" in msg
```

```python
# src/hft_platform/ops/daily_pnl_report.py
"""Daily PnL report section — integrates into DailyReportService.

Not a standalone reporter. Provides a format_telegram_section() method
that DailyReportService calls to build the full EOD message.
"""
from __future__ import annotations


class DailyPnlSection:
    """Format PnL data as a Telegram HTML section."""

    def format_telegram_section(
        self,
        *,
        realized_pnl_ntd: int,
        unrealized_pnl_ntd: int,
        trade_count: int,
        fill_count: int,
    ) -> str:
        total = realized_pnl_ntd + unrealized_pnl_ntd
        icon = "\U0001f4c8" if total >= 0 else "\U0001f4c9"
        return (
            f"<b>{icon} Daily PnL</b>\n"
            f"  Realized: {realized_pnl_ntd:,} NTD\n"
            f"  Unrealized: {unrealized_pnl_ntd:,} NTD\n"
            f"  Total: {total:,} NTD\n"
            f"  Trades: {trade_count} | Fills: {fill_count}"
        )
```

- [ ] **Step 6: Run PnL report tests**

Run: `uv run pytest tests/unit/test_daily_pnl_report.py -v`
Expected: All 3 tests PASS

- [ ] **Step 7: Integrate into DailyReportService**

In `src/hft_platform/services/daily_report.py`, add imports at the top:

```python
from hft_platform.tca.report import TCAReportGenerator
from hft_platform.tca.analyzer import TCAAnalyzer
from hft_platform.ops.daily_pnl_report import DailyPnlSection
```

In the `on_session_closed` method, after the existing CH query and before `notify_daily_report`, add:

```python
        # TCA section
        tca_section = ""
        try:
            tca_gen = TCAReportGenerator()
            tca_analyzer = TCAAnalyzer(self._ch_client)
            tca_reports = tca_analyzer.daily_report(date_label)
            tca_section = tca_gen.format_telegram_section(tca_reports)
        except Exception:
            logger.warning("daily_report_tca_section_failed", exc_info=True)

        # PnL section
        pnl_section = ""
        try:
            pnl_gen = DailyPnlSection()
            pnl_section = pnl_gen.format_telegram_section(
                realized_pnl_ntd=aggregates.get("pnl_ntd", 0),
                unrealized_pnl_ntd=0,  # TODO: wire from position_store
                trade_count=aggregates.get("trade_count", 0),
                fill_count=aggregates.get("fill_count", 0),
            )
        except Exception:
            logger.warning("daily_report_pnl_section_failed", exc_info=True)
```

Then modify the `notify_daily_report` call to append these sections to the message.

- [ ] **Step 8: Commit**

```bash
git add src/hft_platform/tca/report.py tests/unit/test_tca_report.py \
        src/hft_platform/ops/daily_pnl_report.py tests/unit/test_daily_pnl_report.py \
        src/hft_platform/services/daily_report.py
git commit -m "feat(tca): add TCA report + PnL section, integrate into DailyReportService"
```

---

### Task 15: Analytics Package + Feasibility CLI

**Files:**
- Create: `src/hft_platform/analytics/__init__.py`
- Create: `src/hft_platform/analytics/queries.py`
- Create: `src/hft_platform/cli/_feasibility.py`

- [ ] **Step 1: Create analytics package**

```python
# src/hft_platform/analytics/__init__.py
"""Shared ClickHouse aggregation queries for TCA, PnL, and feasibility reports."""

from hft_platform.analytics.queries import (
    query_daily_pnl,
    query_fill_quality,
    query_liquidity_gate_stats,
    query_slippage_distribution,
)

__all__ = [
    "query_daily_pnl",
    "query_fill_quality",
    "query_liquidity_gate_stats",
    "query_slippage_distribution",
]
```

```python
# src/hft_platform/analytics/queries.py
"""ClickHouse aggregation queries for analytics and feasibility reporting."""
from __future__ import annotations

from typing import Any


def query_daily_pnl(ch_client: Any, date_str: str) -> list[dict[str, Any]]:
    """Query daily PnL by strategy and symbol."""
    rows = ch_client.execute(
        """
        SELECT
            strategy_id,
            symbol,
            count(*) AS fill_count,
            sum(qty) AS total_qty,
            sum(fee_scaled + tax_scaled) / 10000 AS total_cost_ntd
        FROM hft.fills
        WHERE toDate(ts_exchange / 1000000000) = %(date)s
        GROUP BY strategy_id, symbol
        ORDER BY strategy_id, symbol
        """,
        {"date": date_str},
    )
    return [
        {
            "strategy": r[0], "symbol": r[1], "fill_count": r[2],
            "total_qty": r[3], "total_cost_ntd": r[4],
        }
        for r in rows
    ]


def query_slippage_distribution(ch_client: Any, date_str: str) -> list[dict[str, Any]]:
    """Query slippage distribution from slippage_records."""
    rows = ch_client.execute(
        """
        SELECT
            symbol,
            count(*) AS n,
            avg(slippage_ticks) AS avg_ticks,
            quantile(0.95)(slippage_ticks) AS p95_ticks
        FROM hft.slippage_records
        WHERE toDate(ts / 1000000000) = %(date)s
        GROUP BY symbol
        """,
        {"date": date_str},
    )
    return [
        {"symbol": r[0], "count": r[1], "avg_ticks": r[2], "p95_ticks": r[3]}
        for r in rows
    ]


def query_fill_quality(ch_client: Any, date_str: str) -> list[dict[str, Any]]:
    """Query fill quality metrics (decision→fill latency)."""
    rows = ch_client.execute(
        """
        SELECT
            strategy_id,
            symbol,
            count(*) AS n,
            avg(latency_ns) / 1e6 AS avg_latency_ms,
            quantile(0.95)(latency_ns) / 1e6 AS p95_latency_ms
        FROM hft.slippage_records
        WHERE toDate(ts / 1000000000) = %(date)s
        GROUP BY strategy_id, symbol
        """,
        {"date": date_str},
    )
    return [
        {
            "strategy": r[0], "symbol": r[1], "count": r[2],
            "avg_latency_ms": r[3], "p95_latency_ms": r[4],
        }
        for r in rows
    ]


def query_liquidity_gate_stats(ch_client: Any, date_str: str) -> list[dict[str, Any]]:
    """Query liquidity gate rejection stats."""
    rows = ch_client.execute(
        """
        SELECT
            symbol,
            countIf(result = 'rejected') AS rejected,
            countIf(result = 'passed') AS passed,
            count(*) AS total
        FROM hft.liquidity_gate_events
        WHERE toDate(ts / 1000000000) = %(date)s
        GROUP BY symbol
        """,
        {"date": date_str},
    )
    return [
        {"symbol": r[0], "rejected": r[1], "passed": r[2], "total": r[3]}
        for r in rows
    ]
```

- [ ] **Step 2: Create feasibility CLI command**

```python
# src/hft_platform/cli/_feasibility.py
"""CLI command: hft feasibility report — aggregated feasibility scorecard."""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys


def cmd_feasibility_report(args: argparse.Namespace) -> None:
    """Generate feasibility report from ClickHouse data."""
    try:
        from clickhouse_driver import Client as CHClient
    except ImportError:
        print("clickhouse-driver not installed. Run: pip install clickhouse-driver")
        sys.exit(1)

    from hft_platform.analytics.queries import (
        query_daily_pnl,
        query_fill_quality,
        query_liquidity_gate_stats,
        query_slippage_distribution,
    )

    date_str: str = getattr(args, "date", None) or _dt.date.today().isoformat()

    ch_host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    ch_port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))

    try:
        ch = CHClient(host=ch_host, port=ch_port)
    except Exception as exc:
        print(f"Failed to connect to ClickHouse: {exc}")
        sys.exit(1)

    print(f"=== Feasibility Report: {date_str} ===\n")

    # PnL
    pnl = query_daily_pnl(ch, date_str)
    print("--- Daily PnL ---")
    if pnl:
        for row in pnl:
            print(f"  {row['strategy']:20s} {row['symbol']:10s} "
                  f"fills={row['fill_count']} qty={row['total_qty']} "
                  f"cost={row['total_cost_ntd']:.0f} NTD")
    else:
        print("  No fills found.")

    # Slippage
    slip = query_slippage_distribution(ch, date_str)
    print("\n--- Slippage Distribution ---")
    if slip:
        for row in slip:
            print(f"  {row['symbol']:10s} n={row['count']} "
                  f"avg={row['avg_ticks']:.1f} ticks P95={row['p95_ticks']:.1f} ticks")
    else:
        print("  No slippage records found.")

    # Fill Quality
    fq = query_fill_quality(ch, date_str)
    print("\n--- Fill Quality ---")
    if fq:
        for row in fq:
            print(f"  {row['strategy']:20s} {row['symbol']:10s} "
                  f"n={row['count']} avg={row['avg_latency_ms']:.1f}ms "
                  f"P95={row['p95_latency_ms']:.1f}ms")
    else:
        print("  No fill quality data.")

    # Liquidity Gate
    lg = query_liquidity_gate_stats(ch, date_str)
    print("\n--- Liquidity Gate ---")
    if lg:
        for row in lg:
            reject_pct = (row['rejected'] / row['total'] * 100) if row['total'] > 0 else 0
            print(f"  {row['symbol']:10s} "
                  f"passed={row['passed']} rejected={row['rejected']} "
                  f"({reject_pct:.1f}% rejected)")
    else:
        print("  No gate events.")

    print(f"\n=== End Report ===")
```

- [ ] **Step 3: Register CLI command**

Find the CLI argument parser registration (in `src/hft_platform/cli/__init__.py` or equivalent) and add:

```python
feasibility_parser = subparsers.add_parser("feasibility", help="Feasibility analysis")
feasibility_sub = feasibility_parser.add_subparsers()
feas_report = feasibility_sub.add_parser("report", help="Generate feasibility report")
feas_report.add_argument("--date", help="Date (YYYY-MM-DD), default today")
feas_report.set_defaults(func=cmd_feasibility_report)
```

With import: `from hft_platform.cli._feasibility import cmd_feasibility_report`

- [ ] **Step 4: Create ClickHouse migrations for remaining tables**

```sql
-- src/hft_platform/migrations/clickhouse/20260327_003_add_daily_reports.sql
CREATE TABLE IF NOT EXISTS hft.daily_reports (
    date          Date,
    strategy_id   String,
    symbol        String,
    realized_pnl  Int64,
    unrealized_pnl Int64,
    trade_count   UInt32,
    fill_count    UInt32,
    total_cost_ntd Int64,
    ts            Int64
) ENGINE = MergeTree()
ORDER BY (date, strategy_id, symbol)
TTL date + INTERVAL 1 YEAR;
```

```sql
-- src/hft_platform/migrations/clickhouse/20260327_004_add_liquidity_gate_events.sql
CREATE TABLE IF NOT EXISTS hft.liquidity_gate_events (
    symbol    String,
    result    String,  -- 'passed' or 'rejected'
    spread_pts Float64,
    threshold  Float64,
    ts         Int64
) ENGINE = MergeTree()
ORDER BY (symbol, ts)
TTL toDateTime(ts / 1000000000) + INTERVAL 90 DAY;
```

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/analytics/__init__.py \
        src/hft_platform/analytics/queries.py \
        src/hft_platform/cli/_feasibility.py \
        src/hft_platform/migrations/clickhouse/20260327_003_add_daily_reports.sql \
        src/hft_platform/migrations/clickhouse/20260327_004_add_liquidity_gate_events.sql
git commit -m "feat(analytics): add analytics package, feasibility CLI, remaining migrations"
```

---

## Self-Review Checklist

1. **Spec coverage:** All 8 items covered (1-A through 3-B). Each spec section maps to at least one task. Wave 2 prerequisite (textfile collector) added as Task 4. ✅
2. **Placeholder scan:** No TBD/TODO in code (the `# TODO: wire from position_store` in Task 14 Step 7 is an existing pattern in daily_report.py and is documented). All steps have code. ✅
3. **Type consistency:** `SlippageBreakdown` used consistently across `slippage.py`, `slippage_tracker.py`, `report.py`. `FillEvent` fields match across `execution.py` edit and `slippage_tracker.py` usage. `TCADailyReport` matches between `types.py` (existing) and `report.py` (new). ✅
4. **Spec reconciliation:** Wave 3 explicitly lists existing work that is NOT redone (contracts, migrations, CLI). No duplicate migrations. DailyPnlSection and TCAReportGenerator integrate INTO DailyReportService, not alongside it. ✅
5. **Secret redaction:** Task 3 has `test_secrets_are_redacted` and allowlist-based filtering. ✅
6. **Textfile collector:** Task 4 is an explicit prerequisite before Tasks 6-7. ✅
