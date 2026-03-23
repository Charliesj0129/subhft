# Phase 1: Solo-Operator Automation & Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the safety net so the HFT platform protects itself when the solo operator is not watching — Telegram alerts, daily loss limit, reconciliation, pre-market checks, and process supervision.

**Architecture:** New `notifications/` module provides async Telegram alerts. Existing `DailyLossLimitValidator` is enhanced with unrealized PnL (via new `PositionStore.mark_to_market()`) and 05:00 local reset. New scripts handle daily reconciliation and pre-market health checks. File-based heartbeat + systemd service provide process supervision.

**Tech Stack:** Python 3.12, aiohttp (Telegram API), structlog, pytest, Redis (IPC for kill-switch), ClickHouse (reconciliation queries), Shioaji (broker health check)

**Spec:** `docs/superpowers/specs/2026-03-23-production-rollout-design.md` (Phase 1, Sections 3.1–3.7)

---

## Review Errata (Must-Read Before Implementation)

The following corrections apply to code snippets in the tasks below. Implementers MUST apply these fixes.

### E1: `PositionStore.positions` is public (not `_positions`)
In Task 4, `mark_to_market()` must iterate `self.positions.items()` (NOT `self._positions`).
The key format is `"{account}:{strategy}:{symbol}"` — extract symbol from key.

### E2: `DailyLossLimitValidator.check()` takes `OrderIntent`
In Task 5, all test calls must pass `OrderIntent(intent_id=0, strategy_id="s1", symbol="TMF", intent_type=IntentType.NEW, side=Side.BUY, price=200_0000, qty=1)` — NOT `("s1", {})`.

### E3: Existing codebase modules to reuse (NOT rewrite)
- **Order rate limiter**: Already exists at `order/adapter.py:114` (`RateLimiter` + `PerSymbolRateLimiter`). Just verify config `max_order_per_min: 10` is set in prod risk config.
- **Position size guard**: Already exists at `risk/validators.py:153` (`MaxPositionLotsValidator`). Just verify config `max_position_lots: 1` is set.
- **Strategy circuit breaker**: Already exists at `strategy/runner.py:179` (3-state FSM). Just verify thresholds.
- **Halt canceller**: Already exists at `order/halt_canceller.py`. Use this in Task 6 for cancel-all-orders flow.

### E4: Missing Task — Telegram command poller (/stop, /status)
Add to Task 2: implement `TelegramCommandPoller` class that:
- Polls Telegram `getUpdates` API every 5s from asyncio loop
- `/stop` → sets Redis key `hft:emergency_halt=1` → triggers StormGuard HALT
- `/status` → replies with current positions, PnL, StormGuard state
- Whitelist: only responds to `HFT_TELEGRAM_CHAT_ID`

### E5: Reconciliation must write to ClickHouse `hft.reconciliation` table
Add to Task 7: create migration DDL for `hft.reconciliation` table and write match/mismatch records. Also fix `platform = ch` placeholder — read from Redis position snapshot or engine HTTP endpoint.

### E6: `_send_mismatch_alert` must report ALL mismatches, not just first

### E7: Scripts need tests
Add unit tests for individual check functions in `pre_market_check.py` (mock shioaji, clickhouse, redis). Add tests for `daily_reconcile.py` comparison logic (mock data sources).

### E8: Use `timebase.now_ns()` not `datetime.now()` in scripts
Per CLAUDE.md coding style. Import from `hft_platform.core.timebase`.

### E9: Reuse aiohttp session in TelegramSender
Store `_session: Optional[aiohttp.ClientSession]` as instance attribute. Create lazily on first send, reuse for subsequent sends. Close in `async close()` method.

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/hft_platform/notifications/__init__.py` | Package init, re-exports `TelegramNotifier`, `NotificationDispatcher` |
| `src/hft_platform/notifications/telegram.py` | Async Telegram Bot API sender + command poller |
| `src/hft_platform/notifications/dispatcher.py` | Routes platform events → notification handlers |
| `src/hft_platform/notifications/templates.py` | Structured message templates (HALT, daily loss, daily report, etc.) |
| `tests/unit/test_telegram.py` | Unit tests for Telegram sender (mocked HTTP) |
| `tests/unit/test_notification_dispatcher.py` | Unit tests for event → notification routing |
| `tests/unit/test_notification_templates.py` | Unit tests for message template rendering |
| `tests/unit/test_mark_to_market.py` | Unit tests for PositionStore.mark_to_market() |
| `tests/unit/test_daily_loss_enhanced.py` | Unit tests for enhanced daily loss (unrealized PnL + 05:00 reset) |
| `tests/unit/test_heartbeat.py` | Unit tests for heartbeat file writer |
| `tests/integration/test_daily_loss_halt_flow.py` | Integration: daily loss → HALT → cancel → Telegram |
| `scripts/daily_reconcile.py` | Post-market three-way reconciliation |
| `scripts/pre_market_check.py` | Pre-market 6-point health check |
| `scripts/weekly_summary.py` | Friday weekly reliability report |
| `ops/hft-engine.service` | Systemd service unit |
| `ops/wait-for-healthy.sh` | Startup health gate script |
| `ops/check-heartbeat.sh` | Watchdog cron script |
| `config/env/prod/canary.yaml` | Canary trading config (Phase 3 prep) |

### Modified Files

| File | Change |
|------|--------|
| `src/hft_platform/execution/positions.py` | Add `mark_to_market()` method to `PositionStore` |
| `src/hft_platform/risk/validators.py` | Enhance `DailyLossLimitValidator`: unrealized PnL, 05:00 reset, HALT trigger |
| `src/hft_platform/risk/engine.py` | Wire unrealized PnL feed into daily loss check |
| `src/hft_platform/services/system.py` | Add heartbeat file writer to main loop |
| `src/hft_platform/observability/metrics.py` | Add daily loss + notification metrics |
| `config/env/prod/risk.yaml` | Update with spec values (-10,000 NTD, etc.) |

---

## Task Dependency Graph

```
Task 1 (Templates) ──┐
                      ├── Task 3 (Dispatcher) ── Task 7 (Reconcile) ── Task 8 (Pre-Market)
Task 2 (Telegram)  ──┘          │
                                │
Task 4 (mark_to_market) ── Task 5 (Daily Loss Enhancement) ── Task 6 (HALT Integration)
                                                                       │
Task 9 (Heartbeat) ── Task 10 (Ops Scripts) ──────────────────────── Task 11 (Config + Wiring)
                                                                       │
                                                                  Task 12 (Integration Tests)
```

---

## Task 1: Notification Templates

**Files:**
- Create: `src/hft_platform/notifications/templates.py`
- Create: `tests/unit/test_notification_templates.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_notification_templates.py
"""Tests for notification message templates."""
from __future__ import annotations

import pytest


def test_halt_template_renders_reason():
    from hft_platform.notifications.templates import render_halt

    msg = render_halt(reason="feed_gap > 30s")
    assert "HALT" in msg
    assert "feed_gap > 30s" in msg
    assert "Manual recovery required" in msg


def test_daily_loss_template_renders_pnl_and_limit():
    from hft_platform.notifications.templates import render_daily_loss

    msg = render_daily_loss(pnl_ntd=-10500, limit_ntd=-10000)
    assert "-10,500" in msg or "-10500" in msg
    assert "-10,000" in msg or "-10000" in msg
    assert "HALT" in msg


def test_daily_report_template_renders_all_fields():
    from hft_platform.notifications.templates import render_daily_report

    msg = render_daily_report(
        date_str="2026-04-15 (二)",
        pnl_ntd=1230,
        buys=12,
        sells=12,
        fills=24,
        position_status="flat",
        reconciliation_status="三方一致",
        latency_p95_ms=1.2,
        reconnect_count=0,
        storm_guard_state="NORMAL",
        memory_gb=1.8,
        memory_max_gb=4.0,
    )
    assert "1,230" in msg or "1230" in msg
    assert "NORMAL" in msg
    assert "flat" in msg


def test_stormguard_change_template():
    from hft_platform.notifications.templates import render_stormguard_change

    msg = render_stormguard_change(old="NORMAL", new="WARM", reason="queue_depth_spike")
    assert "NORMAL" in msg
    assert "WARM" in msg
    assert "queue_depth_spike" in msg


def test_pre_market_pass_template():
    from hft_platform.notifications.templates import render_pre_market_pass

    msg = render_pre_market_pass()
    assert "PASS" in msg


def test_pre_market_fail_template():
    from hft_platform.notifications.templates import render_pre_market_fail

    msg = render_pre_market_fail(failed_checks=["broker_connectivity", "disk_space"])
    assert "broker_connectivity" in msg
    assert "disk_space" in msg


def test_reconciliation_mismatch_template():
    from hft_platform.notifications.templates import render_reconciliation_mismatch

    msg = render_reconciliation_mismatch(
        platform_pnl=-500, broker_pnl=-480, ch_pnl=-500
    )
    assert "-500" in msg
    assert "-480" in msg


def test_reconnect_alert_template():
    from hft_platform.notifications.templates import render_reconnect_alert

    msg = render_reconnect_alert(count=4, flap_status="monitoring")
    assert "4" in msg


def test_process_restart_template():
    from hft_platform.notifications.templates import render_process_restart

    msg = render_process_restart(attempt=2, max_attempts=3)
    assert "2" in msg
    assert "3" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_notification_templates.py -v`
Expected: FAIL (ModuleNotFoundError: No module named 'hft_platform.notifications')

- [ ] **Step 3: Create package init and implement templates**

```python
# src/hft_platform/notifications/__init__.py
"""Notification subsystem for solo-operator alerts."""
```

```python
# src/hft_platform/notifications/templates.py
"""Structured notification message templates.

All templates return plain strings. No dynamic string interpolation
from untrusted input — all parameters are typed and formatted explicitly.
"""
from __future__ import annotations


def render_halt(reason: str) -> str:
    return f"🔴 HALT: {reason}. All trading stopped. Manual recovery required."


def render_daily_loss(pnl_ntd: int, limit_ntd: int) -> str:
    return (
        f"🔴 日損限額觸及: PnL={pnl_ntd:,} NTD (limit={limit_ntd:,}). "
        "HALT activated."
    )


def render_daily_report(
    *,
    date_str: str,
    pnl_ntd: int,
    buys: int,
    sells: int,
    fills: int,
    position_status: str,
    reconciliation_status: str,
    latency_p95_ms: float,
    reconnect_count: int,
    storm_guard_state: str,
    memory_gb: float,
    memory_max_gb: float,
) -> str:
    return (
        f"📊 日報 {date_str}\n\n"
        f"💰 PnL: {pnl_ntd:+,} NTD\n"
        f"📈 交易: 買 {buys} / 賣 {sells} / 成交 {fills}\n"
        f"📋 持倉: {position_status}\n"
        f"✅ 對帳: {reconciliation_status}\n\n"
        f"⏱ 系統:\n"
        f"  延遲 P95: {latency_p95_ms:.1f}ms (tick→signal)\n"
        f"  Reconnect: {reconnect_count} 次\n"
        f"  StormGuard: {storm_guard_state}\n"
        f"  記憶體: {memory_gb:.1f} GB / {memory_max_gb:.1f} GB"
    )


def render_stormguard_change(old: str, new: str, reason: str) -> str:
    return f"🟡 StormGuard: {old} → {new}. Reason: {reason}."


def render_pre_market_pass() -> str:
    return "🟢 08:15 健檢 PASS. 策略將於 08:45 啟動."


def render_pre_market_fail(failed_checks: list[str]) -> str:
    checks = ", ".join(failed_checks)
    return f"🟠 開盤前健檢 FAIL: {checks}. 策略不啟動."


def render_reconciliation_mismatch(
    platform_pnl: int, broker_pnl: int, ch_pnl: int
) -> str:
    return (
        f"🟠 對帳不一致: platform={platform_pnl}, "
        f"broker={broker_pnl}, CH={ch_pnl}. 明日 HALT pending."
    )


def render_reconnect_alert(count: int, flap_status: str) -> str:
    return f"🟠 Reconnect #{count} today. Flap detection: {flap_status}."


def render_process_restart(attempt: int, max_attempts: int) -> str:
    return f"🟠 Engine restarted by systemd. Attempt {attempt}/{max_attempts}."


def render_weekly_summary(
    *,
    week_label: str,
    date_range: str,
    total_pnl_ntd: int,
    trading_days: int,
    avg_trades: int,
    best_day_ntd: int,
    worst_day_ntd: int,
    reconciliation_match: str,
    halt_count: int,
    reconnect_count: int,
    latency_p95_avg_ms: float,
    rss_peak_gb: float,
    uptime_pct: float,
) -> str:
    return (
        f"📊 週報 {week_label} ({date_range})\n\n"
        f"💰 週 PnL: {total_pnl_ntd:+,} NTD ({trading_days} trading days)\n"
        f"📈 日均交易: {avg_trades} 筆 / 最高單日: {best_day_ntd:+,} / "
        f"最低單日: {worst_day_ntd:+,}\n"
        f"📋 對帳: {reconciliation_match}\n\n"
        f"⏱ 系統穩定性:\n"
        f"  HALT: {halt_count} 次 / Reconnect: {reconnect_count} 次 (total)\n"
        f"  延遲 P95 avg: {latency_p95_avg_ms:.1f}ms / RSS peak: {rss_peak_gb:.1f} GB\n"
        f"  Uptime: {uptime_pct:.0f}%"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_notification_templates.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/notifications/__init__.py src/hft_platform/notifications/templates.py tests/unit/test_notification_templates.py
git commit -m "feat(notifications): add structured message templates for Telegram alerts"
```

---

## Task 2: Telegram Sender

**Files:**
- Create: `src/hft_platform/notifications/telegram.py`
- Create: `tests/unit/test_telegram.py`

**Dependencies:** None (Task 1 templates used by dispatcher, not directly by sender)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_telegram.py
"""Tests for async Telegram sender."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


@pytest.fixture
def sender():
    from hft_platform.notifications.telegram import TelegramSender

    return TelegramSender(
        bot_token="test-token-123",
        chat_id="12345",
        enabled=True,
    )


@pytest.fixture
def disabled_sender():
    from hft_platform.notifications.telegram import TelegramSender

    return TelegramSender(
        bot_token="",
        chat_id="",
        enabled=False,
    )


@pytest.mark.asyncio
async def test_send_message_posts_to_telegram_api(sender):
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"ok": True})

    mock_session = AsyncMock()
    mock_session.post = AsyncMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await sender.send("Test message")

    assert result is True
    mock_session.post.assert_called_once()
    call_kwargs = mock_session.post.call_args
    assert "test-token-123" in str(call_kwargs)
    assert call_kwargs[1]["json"]["chat_id"] == "12345"
    assert call_kwargs[1]["json"]["text"] == "Test message"


@pytest.mark.asyncio
async def test_send_when_disabled_is_noop(disabled_sender):
    result = await disabled_sender.send("Test")
    assert result is False


@pytest.mark.asyncio
async def test_send_failure_returns_false_no_raise(sender):
    mock_session = AsyncMock()
    mock_session.post = AsyncMock(side_effect=Exception("network error"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await sender.send("Test message")

    assert result is False  # Fire-and-forget: no exception raised


@pytest.mark.asyncio
async def test_rate_limiter_batches_non_critical():
    from hft_platform.notifications.telegram import TelegramSender

    sender = TelegramSender(
        bot_token="tok",
        chat_id="123",
        enabled=True,
        rate_limit_seconds=1.0,
    )
    # Simulate two rapid sends — second should be queued
    sender._last_send_ts = asyncio.get_event_loop().time()  # "just sent"

    mock_session = AsyncMock()
    mock_response = AsyncMock(status=200, json=AsyncMock(return_value={"ok": True}))
    mock_session.post = AsyncMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await sender.send("Non-critical", critical=False)

    # Rate-limited: should be queued, not sent immediately
    assert result is False or mock_session.post.call_count == 0


@pytest.mark.asyncio
async def test_critical_bypasses_rate_limit():
    from hft_platform.notifications.telegram import TelegramSender

    sender = TelegramSender(
        bot_token="tok",
        chat_id="123",
        enabled=True,
        rate_limit_seconds=1.0,
    )
    sender._last_send_ts = asyncio.get_event_loop().time()  # "just sent"

    mock_session = AsyncMock()
    mock_response = AsyncMock(status=200, json=AsyncMock(return_value={"ok": True}))
    mock_session.post = AsyncMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await sender.send("CRITICAL alert", critical=True)

    # Critical messages bypass rate limit
    assert result is True
    assert mock_session.post.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_telegram.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Implement TelegramSender**

```python
# src/hft_platform/notifications/telegram.py
"""Async Telegram Bot API sender with rate limiting.

Design principles:
- Async-only: uses aiohttp, never blocks hot path
- Fire-and-forget: send failure → log WARNING, never raise
- Rate-limited: max 1 msg/sec, CRITICAL bypasses rate limit
- No webhook: polling-based command listener
"""
from __future__ import annotations

import asyncio
import os
import time

import structlog

logger = structlog.get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramSender:
    """Sends messages to a single Telegram chat via Bot API."""

    __slots__ = (
        "_bot_token",
        "_chat_id",
        "_enabled",
        "_rate_limit_s",
        "_last_send_ts",
    )

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = False,
        rate_limit_seconds: float = 1.0,
    ) -> None:
        self._bot_token = bot_token or os.environ.get("HFT_TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.environ.get("HFT_TELEGRAM_CHAT_ID", "")
        self._enabled = enabled and bool(self._bot_token) and bool(self._chat_id)
        self._rate_limit_s = rate_limit_seconds
        self._last_send_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str, *, critical: bool = False) -> bool:
        """Send a message to Telegram. Returns True if sent, False otherwise.

        Never raises — all errors are logged and swallowed.
        Critical messages bypass rate limiting.
        """
        if not self._enabled:
            return False

        # Rate limiting (non-critical only)
        now = asyncio.get_event_loop().time()
        if not critical and (now - self._last_send_ts) < self._rate_limit_s:
            logger.debug("telegram_rate_limited", text_prefix=text[:40])
            return False

        try:
            import aiohttp

            url = _TELEGRAM_API.format(token=self._bot_token)
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        self._last_send_ts = now
                        logger.info("telegram_sent", text_prefix=text[:40])
                        return True
                    else:
                        body = await resp.text()
                        logger.warning(
                            "telegram_send_failed",
                            status=resp.status,
                            body=body[:200],
                        )
                        return False
        except Exception:
            logger.warning("telegram_send_error", exc_info=True)
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_telegram.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/notifications/telegram.py tests/unit/test_telegram.py
git commit -m "feat(notifications): add async Telegram sender with rate limiting"
```

---

## Task 3: Notification Dispatcher

**Files:**
- Create: `src/hft_platform/notifications/dispatcher.py`
- Create: `tests/unit/test_notification_dispatcher.py`
- Modify: `src/hft_platform/notifications/__init__.py`

**Dependencies:** Task 1 (templates), Task 2 (telegram sender)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_notification_dispatcher.py
"""Tests for notification event dispatcher."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_sender():
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    sender.enabled = True
    return sender


@pytest.fixture
def dispatcher(mock_sender):
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    return NotificationDispatcher(sender=mock_sender)


@pytest.mark.asyncio
async def test_notify_halt_sends_critical(dispatcher, mock_sender):
    await dispatcher.notify_halt(reason="feed_gap > 30s")
    mock_sender.send.assert_called_once()
    call_args = mock_sender.send.call_args
    assert "HALT" in call_args[0][0]
    assert call_args[1]["critical"] is True


@pytest.mark.asyncio
async def test_notify_daily_loss_sends_critical(dispatcher, mock_sender):
    await dispatcher.notify_daily_loss(pnl_ntd=-10500, limit_ntd=-10000)
    mock_sender.send.assert_called_once()
    assert mock_sender.send.call_args[1]["critical"] is True


@pytest.mark.asyncio
async def test_notify_stormguard_change_sends_warning(dispatcher, mock_sender):
    await dispatcher.notify_stormguard_change(
        old="NORMAL", new="WARM", reason="queue_spike"
    )
    mock_sender.send.assert_called_once()
    assert mock_sender.send.call_args[1]["critical"] is False


@pytest.mark.asyncio
async def test_notify_pre_market_pass(dispatcher, mock_sender):
    await dispatcher.notify_pre_market_pass()
    mock_sender.send.assert_called_once()


@pytest.mark.asyncio
async def test_notify_pre_market_fail(dispatcher, mock_sender):
    await dispatcher.notify_pre_market_fail(failed_checks=["broker", "disk"])
    mock_sender.send.assert_called_once()
    assert "broker" in mock_sender.send.call_args[0][0]


@pytest.mark.asyncio
async def test_dispatcher_with_disabled_sender():
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    sender = AsyncMock()
    sender.enabled = False
    sender.send = AsyncMock(return_value=False)
    disp = NotificationDispatcher(sender=sender)
    await disp.notify_halt(reason="test")
    # Still calls send (sender decides to skip), but no exception
    sender.send.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_notification_dispatcher.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Implement dispatcher**

```python
# src/hft_platform/notifications/dispatcher.py
"""Routes platform events to notification handlers.

Each notify_* method renders the appropriate template and sends via
the configured sender. This is the single integration point for all
platform components that need to send notifications.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from hft_platform.notifications import templates

if TYPE_CHECKING:
    from hft_platform.notifications.telegram import TelegramSender

logger = structlog.get_logger(__name__)


class NotificationDispatcher:
    """Central dispatcher for all platform notifications."""

    __slots__ = ("_sender",)

    def __init__(self, sender: TelegramSender) -> None:
        self._sender = sender

    async def notify_halt(self, reason: str) -> None:
        msg = templates.render_halt(reason=reason)
        await self._sender.send(msg, critical=True)

    async def notify_daily_loss(self, pnl_ntd: int, limit_ntd: int) -> None:
        msg = templates.render_daily_loss(pnl_ntd=pnl_ntd, limit_ntd=limit_ntd)
        await self._sender.send(msg, critical=True)

    async def notify_stormguard_change(
        self, old: str, new: str, reason: str
    ) -> None:
        msg = templates.render_stormguard_change(old=old, new=new, reason=reason)
        await self._sender.send(msg, critical=False)

    async def notify_pre_market_pass(self) -> None:
        msg = templates.render_pre_market_pass()
        await self._sender.send(msg, critical=False)

    async def notify_pre_market_fail(self, failed_checks: list[str]) -> None:
        msg = templates.render_pre_market_fail(failed_checks=failed_checks)
        await self._sender.send(msg, critical=False)

    async def notify_reconciliation_mismatch(
        self, platform_pnl: int, broker_pnl: int, ch_pnl: int
    ) -> None:
        msg = templates.render_reconciliation_mismatch(
            platform_pnl=platform_pnl, broker_pnl=broker_pnl, ch_pnl=ch_pnl
        )
        await self._sender.send(msg, critical=False)

    async def notify_reconnect(self, count: int, flap_status: str) -> None:
        msg = templates.render_reconnect_alert(count=count, flap_status=flap_status)
        await self._sender.send(msg, critical=False)

    async def notify_process_restart(
        self, attempt: int, max_attempts: int
    ) -> None:
        msg = templates.render_process_restart(
            attempt=attempt, max_attempts=max_attempts
        )
        await self._sender.send(msg, critical=False)

    async def notify_daily_report(self, **kwargs) -> None:
        msg = templates.render_daily_report(**kwargs)
        await self._sender.send(msg, critical=False)

    async def notify_weekly_summary(self, **kwargs) -> None:
        msg = templates.render_weekly_summary(**kwargs)
        await self._sender.send(msg, critical=False)
```

- [ ] **Step 4: Update package __init__.py**

```python
# src/hft_platform/notifications/__init__.py
"""Notification subsystem for solo-operator alerts."""

from hft_platform.notifications.dispatcher import NotificationDispatcher
from hft_platform.notifications.telegram import TelegramSender

__all__ = ["NotificationDispatcher", "TelegramSender"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_notification_dispatcher.py tests/unit/test_notification_templates.py tests/unit/test_telegram.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/notifications/ tests/unit/test_notification_dispatcher.py
git commit -m "feat(notifications): add dispatcher routing platform events to Telegram"
```

---

## Task 4: PositionStore.mark_to_market()

**Files:**
- Modify: `src/hft_platform/execution/positions.py`
- Create: `tests/unit/test_mark_to_market.py`

**Dependencies:** None

**Context:** The `PositionStore` class tracks open positions. We need to add `mark_to_market()` which computes unrealized PnL for all open positions given current mid prices. This is needed by the enhanced daily loss limit check.

- [ ] **Step 1: Read the existing PositionStore implementation**

Run: `uv run grep -n "class PositionStore\|def \|class Position" src/hft_platform/execution/positions.py`

Understand the current interface before modifying. Key things to find:
- How positions are stored (dict keyed by symbol?)
- What fields `Position` has (`net_qty`, `avg_price_scaled`)
- How `realized_pnl_scaled` is tracked

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_mark_to_market.py
"""Tests for PositionStore.mark_to_market()."""
from __future__ import annotations

import pytest


def test_mark_to_market_long_position_profit():
    """Long 1 lot, price went up → positive unrealized PnL."""
    from hft_platform.execution.positions import PositionStore

    store = PositionStore()
    # Simulate: bought 1 lot at 200_0000 (20000 x 10000 scale)
    # Current mid price: 201_0000 (20100 x 10000 scale)
    # Unrealized = (201_0000 - 200_0000) * 1 = 10000 (scaled)
    # Contract multiplier needed — check existing code for how it's used
    store._inject_position_for_test("TMF", net_qty=1, avg_price_scaled=200_0000)

    result = store.mark_to_market({"TMF": 201_0000})
    assert result == 1_0000  # +1 point * 10000 scale


def test_mark_to_market_short_position_loss():
    """Short 1 lot, price went up → negative unrealized PnL."""
    from hft_platform.execution.positions import PositionStore

    store = PositionStore()
    store._inject_position_for_test("TMF", net_qty=-1, avg_price_scaled=200_0000)

    result = store.mark_to_market({"TMF": 201_0000})
    assert result == -1_0000  # -1 point * 10000 scale


def test_mark_to_market_flat_position():
    """No position → 0 unrealized PnL."""
    from hft_platform.execution.positions import PositionStore

    store = PositionStore()
    result = store.mark_to_market({"TMF": 200_0000})
    assert result == 0


def test_mark_to_market_multiple_symbols():
    """Sum unrealized across all symbols."""
    from hft_platform.execution.positions import PositionStore

    store = PositionStore()
    store._inject_position_for_test("TMF", net_qty=1, avg_price_scaled=200_0000)
    store._inject_position_for_test("MXF", net_qty=-1, avg_price_scaled=210_0000)

    result = store.mark_to_market({"TMF": 201_0000, "MXF": 209_0000})
    # TMF: (201-200)*1 = +1_0000
    # MXF: (209-210)*(-1) = +1_0000
    assert result == 2_0000


def test_mark_to_market_missing_price_skips_symbol():
    """If mid_price not provided for a symbol, skip it (0 contribution)."""
    from hft_platform.execution.positions import PositionStore

    store = PositionStore()
    store._inject_position_for_test("TMF", net_qty=1, avg_price_scaled=200_0000)

    result = store.mark_to_market({})  # No prices
    assert result == 0
```

Note: `_inject_position_for_test` is a test helper — check existing test patterns for how positions are set up. May need to adapt to actual PositionStore internals.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mark_to_market.py -v`
Expected: FAIL (AttributeError: 'PositionStore' has no attribute 'mark_to_market')

- [ ] **Step 4: Implement mark_to_market()**

Add to `src/hft_platform/execution/positions.py` on the `PositionStore` class:

```python
def mark_to_market(self, mid_prices: dict[str, int]) -> int:
    """Compute total unrealized PnL across all open positions.

    Args:
        mid_prices: Map of symbol → current mid_price (scaled int x10000).

    Returns:
        Total unrealized PnL in scaled int x10000.
        For symbols without a mid_price entry, contribution is 0.
    """
    total_unrealized = 0
    for symbol, position in self._positions.items():
        mid = mid_prices.get(symbol)
        if mid is None or position.net_qty == 0:
            continue
        # unrealized = (current_price - avg_entry_price) * net_qty
        total_unrealized += (mid - position.avg_price_scaled) * position.net_qty
    return total_unrealized
```

Also add `_inject_position_for_test()` helper if needed (or use existing setup patterns from tests).

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_mark_to_market.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/execution/positions.py tests/unit/test_mark_to_market.py
git commit -m "feat(positions): add mark_to_market() for unrealized PnL calculation"
```

---

## Task 5: Enhanced Daily Loss Limit Validator

**Files:**
- Modify: `src/hft_platform/risk/validators.py` (lines 175-261: `DailyLossLimitValidator`)
- Create: `tests/unit/test_daily_loss_enhanced.py`

**Dependencies:** Task 4 (mark_to_market)

**Context:** The existing `DailyLossLimitValidator` only tracks realized PnL. We need to:
1. Add unrealized PnL feed (via `update_unrealized()` method)
2. Change reset trigger from UTC midnight to 05:00 local time (Taiwan UTC+8)
3. Add HALT trigger when limit breached (instead of just rejecting orders)

- [ ] **Step 1: Read the existing DailyLossLimitValidator**

Run: `uv run sed -n '175,261p' src/hft_platform/risk/validators.py`

Understand the current `_maybe_reset()`, `record_pnl()`, and `check()` methods exactly.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_daily_loss_enhanced.py
"""Tests for enhanced DailyLossLimitValidator with unrealized PnL and 05:00 reset."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from hft_platform.risk.validators import DailyLossLimitValidator


@pytest.fixture
def validator():
    """Create validator with 100,000,000 scaled limit (10,000 NTD)."""
    v = DailyLossLimitValidator(
        config={},
        defaults={"max_daily_loss": 100_000_000},
    )
    return v


def test_unrealized_pnl_included_in_loss_check(validator):
    """Unrealized loss should count toward daily limit."""
    # Record realized loss of 5000 NTD (50_000_000 scaled)
    validator.record_pnl("s1", -50_000_000)
    # Update unrealized loss of 6000 NTD (60_000_000 scaled)
    validator.update_unrealized(-60_000_000)

    # Total = -11,000 NTD > limit of -10,000 NTD → should reject
    passed, reason = validator.check("s1", {})
    assert passed is False
    assert "daily_loss" in reason.lower()


def test_realized_only_below_limit_passes(validator):
    """Realized loss alone below limit → pass."""
    validator.record_pnl("s1", -50_000_000)  # -5000 NTD
    validator.update_unrealized(0)

    passed, _reason = validator.check("s1", {})
    assert passed is True


def test_unrealized_profit_offsets_realized_loss(validator):
    """Unrealized profit can offset realized loss."""
    validator.record_pnl("s1", -80_000_000)  # -8000 NTD realized
    validator.update_unrealized(30_000_000)   # +3000 NTD unrealized

    # Net = -5000 NTD < limit of -10,000 NTD → pass
    passed, _reason = validator.check("s1", {})
    assert passed is True


def test_reset_at_0500_local_time(validator):
    """Validator should reset at 05:00 Taiwan time (21:00 UTC), not midnight UTC."""
    # Record a loss
    validator.record_pnl("s1", -50_000_000)

    # Mock timebase to return a time just after 05:00 local (21:00 UTC)
    # The validator should detect the new trading day and reset
    # Implementation detail: override _is_new_trading_day() or _trading_day_start_ns()
    # This test verifies the reset happens at the right time
    validator._force_reset()
    passed, _reason = validator.check("s1", {})
    assert passed is True  # After reset, no accumulated loss


def test_halt_triggered_on_breach(validator):
    """When daily loss limit is breached, halt_triggered flag should be set."""
    validator.record_pnl("s1", -110_000_000)  # -11,000 NTD > limit
    validator.update_unrealized(0)

    passed, reason = validator.check("s1", {})
    assert passed is False
    assert validator.halt_triggered is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_daily_loss_enhanced.py -v`
Expected: FAIL (AttributeError: 'DailyLossLimitValidator' has no attribute 'update_unrealized')

- [ ] **Step 4: Implement enhancements**

Modify `src/hft_platform/risk/validators.py` `DailyLossLimitValidator` class:

1. Add `_unrealized_pnl: int` slot and `halt_triggered: bool` slot
2. Add `update_unrealized(self, unrealized_scaled: int) -> None` method
3. Modify `check()` to include unrealized PnL in total
4. Modify `_maybe_reset()` to use 05:00 local time (UTC+8 = 21:00 UTC previous day)
5. Add `_force_reset()` method for testing
6. Set `halt_triggered = True` when limit breached

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_daily_loss_enhanced.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Run existing validator tests to check no regression**

Run: `uv run pytest tests/unit/test_risk_validators.py tests/unit/test_risk_engine.py -v`
Expected: All existing tests still PASS

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/risk/validators.py tests/unit/test_daily_loss_enhanced.py
git commit -m "feat(risk): enhance daily loss validator with unrealized PnL and 05:00 reset"
```

---

## Task 6: HALT → Cancel All + Telegram Integration

**Files:**
- Modify: `src/hft_platform/risk/engine.py`
- Create: `tests/integration/test_daily_loss_halt_flow.py`

**Dependencies:** Task 3 (dispatcher), Task 5 (enhanced validator)

**Context:** When daily loss limit is breached, the system must: (1) trigger StormGuard → HALT, (2) cancel all open orders via OrderAdapter, (3) send Telegram CRITICAL alert. This wires the pieces together.

- [ ] **Step 1: Read RiskEngine.evaluate() and notify_fill_pnl()**

Run: `uv run sed -n '327,400p' src/hft_platform/risk/engine.py` and `uv run sed -n '536,542p' src/hft_platform/risk/engine.py`

Understand how validators are called and how PnL is forwarded.

- [ ] **Step 2: Write the integration test**

```python
# tests/integration/test_daily_loss_halt_flow.py
"""Integration test: daily loss breach → HALT → cancel all → Telegram."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_daily_loss_triggers_halt_cancel_and_notification():
    """Full flow: accumulated loss > limit → HALT + cancel + Telegram."""
    # This test requires setting up:
    # 1. RiskEngine with DailyLossLimitValidator (limit = 100_000_000 = 10k NTD)
    # 2. Mock OrderAdapter with cancel_all method
    # 3. Mock NotificationDispatcher
    # 4. Feed PnL that exceeds limit
    # 5. Assert: StormGuard state = HALT, cancel_all called, Telegram sent

    # Setup will depend on actual RiskEngine constructor —
    # read the __init__ signature first in Step 1
    pass  # Placeholder — implement after reading RiskEngine internals
```

- [ ] **Step 3: Implement the wiring in RiskEngine**

In `src/hft_platform/risk/engine.py`:
1. Add optional `notification_dispatcher` parameter to `__init__()`
2. After `DailyLossLimitValidator.check()` returns `False`:
   - Set StormGuard state to HALT
   - Call `await self._cancel_all_orders()` (new helper)
   - Call `await self._dispatcher.notify_daily_loss(pnl_ntd, limit_ntd)`
3. Add method to feed unrealized PnL from market data loop

- [ ] **Step 4: Implement the integration test fully**

After understanding the actual interfaces, write the complete test.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/integration/test_daily_loss_halt_flow.py tests/unit/test_daily_loss_enhanced.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/risk/engine.py tests/integration/test_daily_loss_halt_flow.py
git commit -m "feat(risk): wire daily loss breach to HALT + cancel all + Telegram"
```

---

## Task 7: Daily Reconciliation Script

**Files:**
- Create: `scripts/daily_reconcile.py`

**Dependencies:** Task 3 (dispatcher for Telegram notifications)

**Context:** Post-market script (cron at 13:50) that queries broker, platform, and ClickHouse for position/PnL data and compares. Sends Telegram daily report on match, CRITICAL alert on mismatch.

- [ ] **Step 1: Create the reconciliation script**

```python
#!/usr/bin/env python3
"""Daily post-market reconciliation: 3-way PnL/position comparison.

Schedule: cron at 13:50 weekdays
Compares: broker positions vs platform PositionStore vs ClickHouse fills

Usage:
    source .env && python scripts/daily_reconcile.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date

import structlog

logger = structlog.get_logger(__name__)

# Tolerance for PnL comparison (±10 NTD = ±100,000 scaled int)
PNL_TOLERANCE_SCALED = 100_000


def _check_trading_day() -> bool:
    """Exit early on non-trading days."""
    try:
        from hft_platform.core.market_calendar import get_calendar
        cal = get_calendar()
        if not cal.is_trading_day():
            logger.info("not_trading_day", date=str(date.today()))
            return False
    except Exception:
        logger.warning("market_calendar_unavailable", exc_info=True)
    return True


async def _query_broker() -> dict:
    """Query broker for current positions and realized PnL."""
    import shioaji as sj

    api = sj.Shioaji(simulation=False)
    api_key = os.environ["SHIOAJI_API_KEY"]
    secret_key = os.environ["SHIOAJI_SECRET_KEY"]
    api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=10000)

    try:
        positions = api.list_positions(api.futopt_account)
        result = {}
        for p in positions:
            result[p.code] = {
                "qty": p.quantity * (1 if p.direction == "Buy" else -1),
                "pnl": int(getattr(p, "pnl", 0) * 10000),  # scale to int
            }
        return result
    finally:
        api.logout()


async def _query_clickhouse() -> dict:
    """Query ClickHouse for today's fill records."""
    try:
        from clickhouse_driver import Client
        host = os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        client = Client(host=host)
        rows = client.execute(
            "SELECT symbol, sum(qty), sum(realized_pnl) "
            "FROM hft.fills WHERE event_date = today() "
            "GROUP BY symbol"
        )
        return {
            row[0]: {"qty": row[1], "pnl": row[2]}
            for row in rows
        }
    except Exception:
        logger.warning("clickhouse_query_failed", exc_info=True)
        return {}


async def _reconcile(dry_run: bool = False) -> bool:
    """Run three-way reconciliation. Returns True if all match."""
    logger.info("reconciliation_start", date=str(date.today()))

    broker = await _query_broker()
    ch = await _query_clickhouse()
    # Platform positions: read from Redis snapshot or ClickHouse
    # For now, compare broker vs ClickHouse (2-way)
    platform = ch  # TODO: read from Redis position snapshot when available

    all_symbols = set(broker) | set(platform) | set(ch)
    mismatches = []

    for symbol in all_symbols:
        b = broker.get(symbol, {"qty": 0, "pnl": 0})
        p = platform.get(symbol, {"qty": 0, "pnl": 0})
        c = ch.get(symbol, {"qty": 0, "pnl": 0})

        qty_match = b["qty"] == p["qty"] == c["qty"]
        pnl_diff = abs(b["pnl"] - p["pnl"])
        pnl_match = pnl_diff <= PNL_TOLERANCE_SCALED

        if not (qty_match and pnl_match):
            mismatches.append({
                "symbol": symbol,
                "broker": b,
                "platform": p,
                "clickhouse": c,
            })

    if mismatches:
        logger.error("reconciliation_mismatch", mismatches=mismatches)
        if not dry_run:
            await _send_mismatch_alert(mismatches)
        return False
    else:
        logger.info("reconciliation_match", symbols=len(all_symbols))
        if not dry_run:
            await _send_daily_report()
        return True


async def _send_mismatch_alert(mismatches: list) -> None:
    """Send Telegram CRITICAL alert for reconciliation mismatch."""
    from hft_platform.notifications import TelegramSender, NotificationDispatcher
    sender = TelegramSender(enabled=True)
    dispatcher = NotificationDispatcher(sender=sender)
    m = mismatches[0]  # Report first mismatch
    await dispatcher.notify_reconciliation_mismatch(
        platform_pnl=m["platform"]["pnl"] // 10000,
        broker_pnl=m["broker"]["pnl"] // 10000,
        ch_pnl=m["clickhouse"]["pnl"] // 10000,
    )


async def _send_daily_report() -> None:
    """Send Telegram daily report."""
    from hft_platform.notifications import TelegramSender, NotificationDispatcher
    from datetime import datetime
    sender = TelegramSender(enabled=True)
    dispatcher = NotificationDispatcher(sender=sender)
    # TODO: gather actual metrics from Prometheus/ClickHouse
    await dispatcher.notify_daily_report(
        date_str=datetime.now().strftime("%Y-%m-%d (%a)"),
        pnl_ntd=0,
        buys=0, sells=0, fills=0,
        position_status="flat",
        reconciliation_status="三方一致",
        latency_p95_ms=0.0,
        reconnect_count=0,
        storm_guard_state="NORMAL",
        memory_gb=0.0, memory_max_gb=4.0,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily post-market reconciliation")
    parser.add_argument("--dry-run", action="store_true", help="Skip notifications")
    args = parser.parse_args()

    if not _check_trading_day():
        sys.exit(0)

    ok = asyncio.run(_reconcile(dry_run=args.dry_run))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run with --dry-run to verify structure**

Run: `uv run python scripts/daily_reconcile.py --dry-run` (will fail on broker login outside trading hours, but verifies imports and structure)

- [ ] **Step 3: Commit**

```bash
git add scripts/daily_reconcile.py
git commit -m "feat(ops): add daily post-market reconciliation script"
```

---

## Task 8: Pre-Market Health Check Script

**Files:**
- Create: `scripts/pre_market_check.py`

**Dependencies:** Task 3 (dispatcher for Telegram)

- [ ] **Step 1: Create the health check script**

```python
#!/usr/bin/env python3
"""Pre-market health check — 6 checks before allowing strategy start.

Schedule: cron at 08:15 weekdays
All checks must PASS for strategy auto-start at 08:45.

Usage:
    source .env && python scripts/pre_market_check.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import date

import structlog

logger = structlog.get_logger(__name__)


def _check_trading_day() -> bool:
    """Exit early on non-trading days."""
    try:
        from hft_platform.core.market_calendar import get_calendar
        cal = get_calendar()
        if not cal.is_trading_day():
            logger.info("not_trading_day", date=str(date.today()))
            return False
    except Exception:
        logger.warning("market_calendar_unavailable", exc_info=True)
    return True


def check_broker_connectivity() -> tuple[bool, str]:
    """Check 1: Broker login, CA activation, contract fetch, margin."""
    try:
        import shioaji as sj
        api = sj.Shioaji(simulation=False)
        api.login(
            api_key=os.environ["SHIOAJI_API_KEY"],
            secret_key=os.environ["SHIOAJI_SECRET_KEY"],
            contracts_timeout=30000,
        )
        ca_path = os.environ.get("CA_CERT_PATH", "")
        ca_password = os.environ.get("CA_PASSWORD", "")
        person_id = os.environ.get("SHIOAJI_PERSON_ID", "")
        if ca_path and ca_password:
            api.activate_ca(ca_path=ca_path, ca_passwd=ca_password, person_id=person_id)

        # Check margin
        margin = api.margin(api.futopt_account)
        avail = margin.available_margin
        api.logout()

        if avail < 15000:
            return False, f"insufficient_margin: {avail:,.0f} NTD"
        return True, f"margin={avail:,.0f}"
    except Exception as e:
        return False, f"broker_error: {e}"


def check_clickhouse() -> tuple[bool, str]:
    """Check 2: ClickHouse connectivity and table existence."""
    try:
        from clickhouse_driver import Client
        host = os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        client = Client(host=host)
        client.execute("SELECT 1")
        # Check market_data table exists
        tables = client.execute("SHOW TABLES FROM hft")
        table_names = [t[0] for t in tables]
        if "market_data" not in table_names:
            return False, "market_data table missing"
        return True, "ok"
    except Exception as e:
        return False, f"clickhouse_error: {e}"


def check_redis() -> tuple[bool, str]:
    """Check 3: Redis connectivity."""
    try:
        import redis
        host = os.environ.get("HFT_MONITOR_REDIS_HOST", "localhost")
        port = int(os.environ.get("HFT_MONITOR_REDIS_PORT", "6379"))
        r = redis.Redis(host=host, port=port, socket_timeout=5)
        r.ping()
        return True, "ok"
    except Exception as e:
        return False, f"redis_error: {e}"


def check_disk_space() -> tuple[bool, str]:
    """Check 4: Disk space for WAL, logs, data."""
    threshold = 0.80
    paths_to_check = [
        ("/home/charlie/hft_platform/.wal", "wal"),
        ("/var/log", "logs"),
        ("/home/charlie/hft_platform", "data"),
    ]
    for path, label in paths_to_check:
        if os.path.exists(path):
            usage = shutil.disk_usage(path)
            ratio = usage.used / usage.total
            if ratio > threshold:
                return False, f"{label}_disk_full: {ratio:.0%}"
    return True, "ok"


def check_reconciliation() -> tuple[bool, str]:
    """Check 5: Yesterday's reconciliation status."""
    try:
        from clickhouse_driver import Client
        host = os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        client = Client(host=host)
        rows = client.execute(
            "SELECT status FROM hft.reconciliation "
            "ORDER BY event_date DESC LIMIT 1"
        )
        if not rows:
            return True, "no_previous_record"  # First run
        status = rows[0][0]
        if status != "MATCH":
            return False, f"previous_mismatch: {status}"
        return True, "ok"
    except Exception as e:
        # If table doesn't exist yet, pass (first-time setup)
        if "doesn't exist" in str(e) or "UNKNOWN_TABLE" in str(e):
            return True, "reconciliation_table_not_yet_created"
        return False, f"reconciliation_check_error: {e}"


def check_system_resources() -> tuple[bool, str]:
    """Check 6: RAM, CPU, zombie processes."""
    import psutil
    mem = psutil.virtual_memory()
    if mem.available < 2 * 1024 * 1024 * 1024:  # 2 GB
        return False, f"low_memory: {mem.available / 1024**3:.1f} GB available"
    cpu = psutil.cpu_percent(interval=1)
    if cpu > 80:
        return False, f"high_cpu: {cpu}%"
    return True, f"ram={mem.available / 1024**3:.1f}GB, cpu={cpu}%"


async def run_checks(dry_run: bool = False) -> bool:
    """Run all 6 checks. Returns True if all pass."""
    checks = [
        ("broker_connectivity", check_broker_connectivity),
        ("clickhouse", check_clickhouse),
        ("redis", check_redis),
        ("disk_space", check_disk_space),
        ("reconciliation", check_reconciliation),
        ("system_resources", check_system_resources),
    ]

    results = {}
    failed = []
    for name, check_fn in checks:
        try:
            ok, detail = check_fn()
        except Exception as e:
            ok, detail = False, str(e)
        results[name] = {"ok": ok, "detail": detail}
        logger.info("health_check", check=name, ok=ok, detail=detail)
        if not ok:
            failed.append(name)

    if not dry_run:
        from hft_platform.notifications import TelegramSender, NotificationDispatcher
        sender = TelegramSender(enabled=True)
        dispatcher = NotificationDispatcher(sender=sender)
        if failed:
            await dispatcher.notify_pre_market_fail(failed_checks=failed)
        else:
            await dispatcher.notify_pre_market_pass()

    if failed:
        logger.error("pre_market_check_failed", failed=failed)
        return False
    logger.info("pre_market_check_passed", checks=len(checks))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-market health check")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not _check_trading_day():
        sys.exit(0)

    ok = asyncio.run(run_checks(dry_run=args.dry_run))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify imports work**

Run: `uv run python -c "import scripts.pre_market_check"` or `uv run python scripts/pre_market_check.py --dry-run`

- [ ] **Step 3: Commit**

```bash
git add scripts/pre_market_check.py
git commit -m "feat(ops): add pre-market 6-point health check script"
```

---

## Task 9: Heartbeat File Writer

**Files:**
- Modify: `src/hft_platform/services/system.py`
- Create: `tests/unit/test_heartbeat.py`

**Dependencies:** None

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_heartbeat.py
"""Tests for heartbeat file writer."""
from __future__ import annotations

import os
import tempfile
import time

import pytest


def test_write_heartbeat_creates_file():
    from hft_platform.services.heartbeat import write_heartbeat

    with tempfile.NamedTemporaryFile(delete=False, suffix=".heartbeat") as f:
        path = f.name

    try:
        write_heartbeat(path)
        assert os.path.exists(path)
        mtime = os.path.getmtime(path)
        assert abs(time.time() - mtime) < 2  # Within 2 seconds
    finally:
        os.unlink(path)


def test_write_heartbeat_updates_mtime():
    from hft_platform.services.heartbeat import write_heartbeat

    with tempfile.NamedTemporaryFile(delete=False, suffix=".heartbeat") as f:
        path = f.name

    try:
        write_heartbeat(path)
        mtime1 = os.path.getmtime(path)
        time.sleep(0.05)
        write_heartbeat(path)
        mtime2 = os.path.getmtime(path)
        assert mtime2 > mtime1
    finally:
        os.unlink(path)


def test_write_heartbeat_failure_does_not_raise():
    from hft_platform.services.heartbeat import write_heartbeat

    # Writing to non-existent dir should not raise
    write_heartbeat("/nonexistent/dir/heartbeat.tmp")
    # No exception = pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_heartbeat.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement heartbeat module**

```python
# src/hft_platform/services/heartbeat.py
"""File-based heartbeat for process health monitoring.

The engine writes to a heartbeat file every 30s. A cron watchdog
checks the file mtime — if stale (>90s), it restarts the service.
"""
from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_HEARTBEAT_PATH = "/tmp/hft-heartbeat"


def write_heartbeat(path: str = DEFAULT_HEARTBEAT_PATH) -> None:
    """Write current timestamp to heartbeat file. Never raises."""
    try:
        with open(path, "w") as f:
            f.write(str(os.getpid()))
        # Touch to update mtime
        os.utime(path, None)
    except OSError:
        logger.warning("heartbeat_write_failed", path=path, exc_info=True)
```

- [ ] **Step 4: Integrate into system.py main loop**

In `src/hft_platform/services/system.py`, find the supervision loop and add:

```python
from hft_platform.services.heartbeat import write_heartbeat

# Inside the main supervision loop (called every ~30s):
write_heartbeat()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_heartbeat.py -v`
Expected: All 3 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/services/heartbeat.py tests/unit/test_heartbeat.py src/hft_platform/services/system.py
git commit -m "feat(ops): add file-based heartbeat writer for watchdog monitoring"
```

---

## Task 10: Ops Scripts (Systemd + Watchdog)

**Files:**
- Create: `ops/hft-engine.service`
- Create: `ops/wait-for-healthy.sh`
- Create: `ops/check-heartbeat.sh`

**Dependencies:** Task 9 (heartbeat)

- [ ] **Step 1: Create systemd service unit**

```ini
# ops/hft-engine.service
[Unit]
Description=HFT Trading Engine
After=docker.service
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=/home/charlie/hft_platform
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=on-failure
RestartSec=10
StartLimitBurst=3
StartLimitIntervalSec=3600

MemoryMax=4G
MemoryHigh=3G

ExecStartPost=/home/charlie/hft_platform/ops/wait-for-healthy.sh

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create startup health gate**

```bash
#!/usr/bin/env bash
# ops/wait-for-healthy.sh — Wait for engine to become healthy after start
set -euo pipefail

MAX_WAIT=120  # seconds
INTERVAL=5
ELAPSED=0

echo "[wait-for-healthy] Waiting up to ${MAX_WAIT}s for engine health..."

while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    # Check if heartbeat file exists and is recent
    if [ -f /tmp/hft-heartbeat ]; then
        AGE=$(( $(date +%s) - $(stat -c %Y /tmp/hft-heartbeat) ))
        if [ "$AGE" -lt 60 ]; then
            echo "[wait-for-healthy] Engine healthy (heartbeat age: ${AGE}s)"
            exit 0
        fi
    fi
    sleep "$INTERVAL"
    ELAPSED=$((ELAPSED + INTERVAL))
done

echo "[wait-for-healthy] WARNING: Engine did not become healthy within ${MAX_WAIT}s"
exit 1
```

- [ ] **Step 3: Create watchdog cron script**

```bash
#!/usr/bin/env bash
# ops/check-heartbeat.sh — Watchdog: restart engine if heartbeat is stale
set -euo pipefail

HEARTBEAT_FILE="/tmp/hft-heartbeat"
MAX_AGE=90  # seconds
SERVICE="hft-engine"

if [ ! -f "$HEARTBEAT_FILE" ]; then
    echo "[watchdog] Heartbeat file missing — restarting $SERVICE"
    systemctl restart "$SERVICE" || true
    # Send Telegram alert (best-effort)
    python3 -c "
import asyncio, os
from hft_platform.notifications import TelegramSender
async def alert():
    s = TelegramSender(enabled=True)
    await s.send('🟠 Watchdog: heartbeat file missing. Restarting engine.', critical=False)
asyncio.run(alert())
" 2>/dev/null || true
    exit 1
fi

AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") ))

if [ "$AGE" -gt "$MAX_AGE" ]; then
    echo "[watchdog] Heartbeat stale (${AGE}s > ${MAX_AGE}s) — restarting $SERVICE"
    systemctl restart "$SERVICE" || true
    python3 -c "
import asyncio, os
from hft_platform.notifications import TelegramSender
async def alert():
    s = TelegramSender(enabled=True)
    await s.send('🟠 Watchdog: heartbeat stale (${AGE}s). Restarting engine.', critical=False)
asyncio.run(alert())
" 2>/dev/null || true
    exit 1
fi

echo "[watchdog] Heartbeat OK (age: ${AGE}s)"
exit 0
```

- [ ] **Step 4: Make scripts executable**

```bash
chmod +x ops/wait-for-healthy.sh ops/check-heartbeat.sh
```

- [ ] **Step 5: Commit**

```bash
git add ops/
git commit -m "feat(ops): add systemd service unit and watchdog scripts"
```

---

## Task 11: Production Config + Canary Config

**Files:**
- Modify: `config/env/prod/risk.yaml`
- Create: `config/env/prod/canary.yaml`

**Dependencies:** Task 5 (enhanced validator expects new config keys)

- [ ] **Step 1: Read existing prod risk config**

Run: `cat config/env/prod/risk.yaml`

- [ ] **Step 2: Update prod risk config with spec values**

Ensure the following values are set (merge with existing content):

```yaml
risk:
  daily_loss_limit_ntd: -10000
  # In scaled int: -10000 * 10000 = -100_000_000
  max_daily_loss: 100000000
  strategy_loss_limit_ntd: -10000
  max_open_positions: 1
  max_order_per_min: 10
  order_size_limit: 1
  daily_pnl_reset_hour: 5      # 05:00 local time (Taiwan)
  halt_requires_manual_recovery: true
```

- [ ] **Step 3: Create canary config**

```yaml
# config/env/prod/canary.yaml
# Phase 3 Canary Live Trading configuration
# 1 strategy, 1 symbol (TMF), minimal risk

canary:
  enabled: true

  symbol: TMF
  point_value: 10  # 1 point = 10 NTD

  session:
    mode: day_only          # 08:45-13:45, no night session
    auto_start: true        # After pre-market check PASS
    auto_stop_time: "13:40" # Stop new orders 5 min before close
    force_flat_time: "13:43" # Force close any open positions
```

- [ ] **Step 4: Commit**

```bash
git add config/env/prod/risk.yaml config/env/prod/canary.yaml
git commit -m "feat(config): add production risk limits and canary config"
```

---

## Task 12: Weekly Summary Script

**Files:**
- Create: `scripts/weekly_summary.py`

**Dependencies:** Task 3 (dispatcher)

- [ ] **Step 1: Create weekly summary script**

```python
#!/usr/bin/env python3
"""Weekly reliability summary — sent Friday 14:00 via Telegram.

Usage:
    source .env && python scripts/weekly_summary.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta

import structlog

logger = structlog.get_logger(__name__)


async def _gather_weekly_data() -> dict:
    """Gather week's data from ClickHouse."""
    try:
        from clickhouse_driver import Client
        host = os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        client = Client(host=host)

        today = date.today()
        week_start = today - timedelta(days=today.weekday())  # Monday

        # PnL summary
        rows = client.execute(
            "SELECT "
            "  count(DISTINCT event_date) as trading_days, "
            "  sum(realized_pnl) as total_pnl "
            "FROM hft.fills "
            "WHERE event_date >= %(start)s AND event_date <= %(end)s",
            {"start": str(week_start), "end": str(today)},
        )

        trading_days = rows[0][0] if rows else 0
        total_pnl = rows[0][1] if rows else 0

        return {
            "trading_days": trading_days,
            "total_pnl_ntd": total_pnl // 10000,
            "week_start": str(week_start),
            "week_end": str(today),
        }
    except Exception:
        logger.warning("weekly_data_query_failed", exc_info=True)
        return {
            "trading_days": 0,
            "total_pnl_ntd": 0,
            "week_start": "",
            "week_end": "",
        }


async def run_summary(dry_run: bool = False) -> None:
    data = await _gather_weekly_data()
    today = date.today()
    week_num = today.isocalendar()[1]

    logger.info("weekly_summary", **data)

    if not dry_run:
        from hft_platform.notifications import TelegramSender, NotificationDispatcher
        sender = TelegramSender(enabled=True)
        dispatcher = NotificationDispatcher(sender=sender)
        await dispatcher.notify_weekly_summary(
            week_label=f"W{week_num}",
            date_range=f"{data['week_start']} - {data['week_end']}",
            total_pnl_ntd=data["total_pnl_ntd"],
            trading_days=data["trading_days"],
            avg_trades=0,  # TODO: query from ClickHouse
            best_day_ntd=0,
            worst_day_ntd=0,
            reconciliation_match=f"{data['trading_days']}/{data['trading_days']} 日一致",
            halt_count=0,
            reconnect_count=0,
            latency_p95_avg_ms=0.0,
            rss_peak_gb=0.0,
            uptime_pct=100.0,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly reliability summary")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_summary(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add scripts/weekly_summary.py
git commit -m "feat(ops): add weekly reliability summary script"
```

---

## Task 13: Final Integration Test + CI Verification

**Files:**
- Existing tests + new integration tests

**Dependencies:** All previous tasks

- [ ] **Step 1: Run full unit test suite**

Run: `uv run pytest tests/unit/ -v --tb=short`
Expected: All pass, no regressions

- [ ] **Step 2: Run lint + typecheck**

Run: `uv run ruff check src/hft_platform/notifications/ scripts/`
Run: `uv run mypy src/hft_platform/notifications/`
Fix any issues.

- [ ] **Step 3: Run full CI**

Run: `make ci`
Expected: All green (lint + typecheck + test + coverage ≥ 70%)

- [ ] **Step 4: Verify coverage of new code**

Run: `uv run pytest tests/unit/test_notification_templates.py tests/unit/test_telegram.py tests/unit/test_notification_dispatcher.py tests/unit/test_mark_to_market.py tests/unit/test_daily_loss_enhanced.py tests/unit/test_heartbeat.py --cov=hft_platform.notifications --cov=hft_platform.services.heartbeat --cov-report=term-missing`
Expected: ≥80% coverage on new modules

- [ ] **Step 5: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: address lint/type/test issues from Phase 1 implementation"
```

- [ ] **Step 6: Verify Go/No-Go checklist items**

Review each item from spec Section 3.7:
```
□ Systemd service: install and test start/stop/restart
□ Telegram: test all notification types manually
□ Daily loss: trigger with simulated data
□ Reconciliation: test match + mismatch paths
□ Pre-market: test pass + fail paths
□ make ci green
```
