# P1-A + P1-E: Solo Operator Safety + Resilience Docs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete solo operator safety features (margin monitoring, notification redundancy, live mode guard) and resilience documentation (quarterly drill SOP, reconnect burn-in guide).

**Architecture:** Extends existing AutonomyMonitor with margin rule, adds webhook fallback channel to NotificationDispatcher, adds startup confirmation guard. Documentation tasks produce runbooks and scripts.

**Tech Stack:** Python 3.12, asyncio, structlog, aiohttp (webhook)

**Spec:** `docs/superpowers/specs/2026-03-25-operational-readiness-assessment.md` (items S1, S2, S3, S5, R3, R4)

---

## Task 1: S5 — Live Mode Startup Guard

Smallest task — adds `HFT_LIVE_CONFIRM` env var requirement when `HFT_ORDER_MODE=live`.

**Files:**
- Modify: `src/hft_platform/services/bootstrap.py` (add confirmation check in `validate_order_mode_safety()`)
- Test: `tests/unit/test_live_confirm_guard.py`

### Implementation

In `validate_order_mode_safety()` (bootstrap.py ~line 178), after the existing checks, add:

```python
# Live mode double-confirmation gate
if order_mode in {"live", "real"}:
    confirm = os.getenv("HFT_LIVE_CONFIRM", "").strip().lower()
    if confirm != "yes-i-know":
        logger.critical(
            "LIVE_MODE_BLOCKED: Set HFT_LIVE_CONFIRM=yes-i-know to confirm live trading",
            order_mode=order_mode,
        )
        raise SystemExit(1)
    logger.warning("live_mode_confirmed", order_mode=order_mode)
```

### Tests (3)

- test_live_mode_without_confirm_exits: `HFT_ORDER_MODE=live` + no `HFT_LIVE_CONFIRM` → SystemExit
- test_live_mode_with_confirm_passes: `HFT_ORDER_MODE=live` + `HFT_LIVE_CONFIRM=yes-i-know` → no exit
- test_sim_mode_no_confirm_needed: `HFT_ORDER_MODE=sim` → no confirm needed

---

## Task 2: S3 — Webhook Notification Fallback

Adds a `WebhookSender` as a secondary notification channel. Critical alerts are sent to both Telegram + webhook.

**Files:**
- Create: `src/hft_platform/notifications/webhook.py`
- Modify: `src/hft_platform/notifications/dispatcher.py` (add fallback sender)
- Test: `tests/unit/test_webhook_sender.py`

### WebhookSender

```python
class WebhookSender:
    """Simple webhook notification sender (Discord/LINE/generic)."""
    __slots__ = ("_url", "_session", "_timeout")

    def __init__(self, url: str, timeout: float = 10.0) -> None: ...
    async def send(self, text: str) -> bool:
        """POST JSON {"content": text} to webhook URL. Returns True on success."""
```

Configured via `HFT_WEBHOOK_URL` env var. If not set, webhook is disabled (no-op).

### Dispatcher Changes

Add `_fallback_sender` to dispatcher. On critical notifications (`notify_halt`, `notify_daily_loss`), send to both primary (Telegram) and fallback (webhook).

### Tests (4)

- test_webhook_send_success: Mock aiohttp, verify POST
- test_webhook_send_failure_no_crash: Connection error → returns False
- test_webhook_disabled_when_no_url: No env var → no-op
- test_dispatcher_critical_sends_to_both: Verify both telegram + webhook called

---

## Task 3: S1 — Margin Monitoring Rule

Adds a margin monitoring rule to AutonomyMonitor that polls broker margin API, alerts at configurable thresholds, and enters reduce-only when critical.

**Files:**
- Create: `src/hft_platform/ops/margin_monitor.py` (margin check logic, separate from AutonomyMonitor)
- Modify: `src/hft_platform/ops/autonomy_monitor.py` (add margin check call in monitor loop)
- Modify: `src/hft_platform/notifications/templates.py` (add margin alert template)
- Modify: `src/hft_platform/notifications/dispatcher.py` (add `notify_margin_warning`, `notify_margin_critical`)
- Test: `tests/unit/test_margin_monitor.py`

### MarginMonitor

```python
class MarginMonitor:
    """Monitors broker margin ratio and triggers alerts/actions."""
    __slots__ = ("_broker_client", "_warn_ratio", "_critical_ratio", "_poll_interval_s",
                 "_last_poll_ns", "_last_ratio", "_in_warning")

    def __init__(self, broker_client, warn_ratio=0.80, critical_ratio=0.90, poll_interval_s=30): ...

    async def check(self) -> MarginCheckResult:
        """Poll broker margin. Returns action needed."""
        # Calls broker_client.get_margin() (already exists in broker protocol)
        # Returns MarginCheckResult(ratio, action: "ok"|"warn"|"critical"|"error")
```

Config via env vars: `HFT_MARGIN_WARN_RATIO=0.80`, `HFT_MARGIN_CRITICAL_RATIO=0.90`, `HFT_MARGIN_POLL_INTERVAL_S=30`.

Thresholds: margin_used/margin_available ratio.
- < warn_ratio → OK
- ≥ warn_ratio → notify_margin_warning (once per transition)
- ≥ critical_ratio → enter_reduce_only + notify_margin_critical

### Tests (5)

- test_ok_when_below_warn: ratio < 0.8 → OK
- test_warn_at_threshold: ratio ≥ 0.8 → warning action
- test_critical_at_threshold: ratio ≥ 0.9 → critical action
- test_broker_failure_returns_error: get_margin raises → error (no crash)
- test_respects_poll_interval: skips check if within interval

---

## Task 4: S2 — Backup Cron Script

BackupManager exists. Just add a cron-ready wrapper script.

**Files:**
- Create: `scripts/daily-backup.sh`

### Script

```bash
#!/usr/bin/env bash
# Run daily ClickHouse backup via BackupManager.
# Crontab: 0 17 * * 1-5 /path/to/scripts/daily-backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python -c "
import asyncio
from hft_platform.ops.backup import BackupManager
async def main():
    mgr = BackupManager()
    await mgr.run_daily()
asyncio.run(main())
"
```

No tests needed — wrapper script only.

---

## Task 5: R3 — Quarterly Chaos Drill SOP

Creates a comprehensive quarterly drill runbook document.

**Files:**
- Create: `docs/runbooks/quarterly-chaos-drill.md`
- Create: `scripts/run-chaos-drill.sh` (automated orchestrator)

### SOP Document

Comprehensive runbook covering:
1. Pre-drill checklist (backup CH, notify team, confirm sim mode)
2. For each of the 5 playbooks: step-by-step execution, expected behavior, pass/fail criteria
3. Post-drill checklist (verify all services recovered, review metrics)
4. Sign-off process (date, operator, results, notes)

### Automated Script

```bash
#!/usr/bin/env bash
# Run all 5 chaos playbooks with timing and summary report.
set -euo pipefail
echo "=== Quarterly Chaos Drill ==="
echo "Date: $(date -Iseconds)"
uv run pytest tests/chaos/test_playbook_*.py -v --no-cov --tb=short 2>&1 | tee /tmp/chaos-drill-$(date +%Y%m%d).log
echo "=== Drill Complete ==="
```

---

## Task 6: R4 — Reconnect Burn-In Guide

Creates documentation and a metrics collection script for reconnect burn-in validation.

**Files:**
- Create: `docs/runbooks/reconnect-burn-in-guide.md`
- Create: `scripts/reconnect-burn-in-report.sh` (Prometheus query script)

### Guide Document

Covers:
1. Purpose: Validate reconnect reliability before production go-live
2. Procedure: Run 5 trading days in sim mode, collect metrics
3. Metrics to observe: `feed_reconnect_total`, `feed_reconnect_timeout_total`, quote recovery time
4. Pass criteria: reconnect success rate ≥ 99%, P95 recovery ≤ 5s
5. Report template: table with day/reconnects/success_rate/p95_recovery

### Metrics Script

Queries Prometheus for reconnect metrics over 5 days and prints summary.

---

## Summary

| Task | Item | Files Created | Files Modified | Tests |
|------|------|---------------|----------------|-------|
| 1 | S5 Live Guard | — | `bootstrap.py` | 3 |
| 2 | S3 Webhook | `notifications/webhook.py` | `dispatcher.py` | 4 |
| 3 | S1 Margin Monitor | `ops/margin_monitor.py` | `autonomy_monitor.py`, `templates.py`, `dispatcher.py` | 5 |
| 4 | S2 Backup Cron | `scripts/daily-backup.sh` | — | 0 |
| 5 | R3 Chaos Drill SOP | `docs/runbooks/quarterly-chaos-drill.md`, `scripts/run-chaos-drill.sh` | — | 0 |
| 6 | R4 Burn-In Guide | `docs/runbooks/reconnect-burn-in-guide.md`, `scripts/reconnect-burn-in-report.sh` | — | 0 |
| **Total** | | **7 new files** | **5 modified** | **12 tests** |
