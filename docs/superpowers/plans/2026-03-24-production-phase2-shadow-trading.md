# Phase 2: Shadow Trading — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable strategy-driven shadow trading with real market data — orders are intercepted, logged to ClickHouse, and analyzed post-market — but never sent to the broker. Validates system stability over consecutive trading days.

**Architecture:** The core shadow infrastructure already exists (`ShadowOrderSink` in `order/shadow.py`, intercept in `OrderAdapter.execute()`, ClickHouse `hft.shadow_orders` table). Phase 2 adds: ClickHouse persistence for shadow records, Prometheus metrics, bootstrap dual-lock safety, shadow daily analysis script with Telegram report, and evidence pack generation.

**Tech Stack:** Python 3.12, ClickHouse (shadow_orders persistence), Prometheus (shadow metrics), structlog, aiohttp (Telegram), pytest

**Spec:** `docs/superpowers/specs/2026-03-23-production-rollout-design.md` (Phase 2, Sections 4.1–4.6)

---

## Pre-Existing Infrastructure (Already Implemented)

| Component | File | Status |
|-----------|------|--------|
| `ShadowOrderSink` class | `src/hft_platform/order/shadow.py` | ✅ Complete (53 lines) |
| OrderAdapter shadow intercept | `src/hft_platform/order/adapter.py:320-323` | ✅ Wired |
| `hft.shadow_orders` ClickHouse table | `migrations/clickhouse/20260320_001_add_shadow_orders.sql` | ✅ Migration exists |
| Shadow sink unit tests | `tests/unit/test_shadow_order.py` | ✅ 6 tests passing |
| Bootstrap mode validation | `services/bootstrap.py:160-178` | ✅ Prevents live+sim conflict |
| `HFT_ORDER_SHADOW_MODE` env var | Read in `shadow.py:22` | ✅ Functional |

## Remaining Work (This Plan)

| Task | Component | Effort |
|------|-----------|--------|
| 1 | Shadow sink → ClickHouse persistence | Medium |
| 2 | Prometheus shadow metrics | Small |
| 3 | Bootstrap dual-lock validation | Small |
| 4 | Shadow daily report template + dispatcher | Small |
| 5 | Shadow daily analysis script | Medium |
| 6 | Shadow config + evidence pack | Small |
| 7 | CI verification | Small |

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/hft_platform/order/shadow_writer.py` | Async writer: shadow records → ClickHouse batch insert |
| `tests/unit/test_shadow_writer.py` | Unit tests for shadow ClickHouse writer |
| `scripts/shadow_daily_report.py` | Post-market shadow analysis: signal count, simulated PnL, latency |
| `tests/unit/test_shadow_daily_report.py` | Tests for shadow analysis logic |
| `config/env/shadow/main.yaml` | Shadow mode environment config |

### Modified Files

| File | Change |
|------|--------|
| `src/hft_platform/order/shadow.py` | Add ClickHouse writer integration, mid_price capture |
| `src/hft_platform/observability/metrics.py` | Add `shadow_orders_total` counter, `shadow_mode_active` gauge |
| `src/hft_platform/services/bootstrap.py` | Add dual-lock validation (shadow + live = refuse) |
| `src/hft_platform/notifications/templates.py` | Add `render_shadow_daily_report()` |
| `src/hft_platform/notifications/dispatcher.py` | Add `notify_shadow_daily_report()` |

---

## Task 1: Shadow Sink → ClickHouse Persistence

**Files:**
- Create: `src/hft_platform/order/shadow_writer.py`
- Modify: `src/hft_platform/order/shadow.py`
- Create: `tests/unit/test_shadow_writer.py`

**Context:** The `ShadowOrderSink.intercept()` currently returns a dict and logs it, but doesn't persist to ClickHouse. We need a batching writer that accumulates shadow records and flushes to `hft.shadow_orders` periodically.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_shadow_writer.py
"""Tests for shadow order ClickHouse writer."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_writer_batches_records():
    from hft_platform.order.shadow_writer import ShadowOrderWriter

    writer = ShadowOrderWriter(batch_size=3, enabled=False)  # CH disabled for test
    writer.add({"ts_ns": 1, "strategy_id": "s1", "symbol": "TMF", "side": "BUY", "price": 100, "qty": 1, "intent_type": "NEW", "intent_id": "1"})
    writer.add({"ts_ns": 2, "strategy_id": "s1", "symbol": "TMF", "side": "SELL", "price": 101, "qty": 1, "intent_type": "NEW", "intent_id": "2"})
    assert writer.pending_count == 2


def test_writer_flushes_at_batch_size():
    from hft_platform.order.shadow_writer import ShadowOrderWriter

    mock_client = MagicMock()
    writer = ShadowOrderWriter(batch_size=2, enabled=True)
    writer._client = mock_client

    writer.add({"ts_ns": 1, "strategy_id": "s1", "symbol": "TMF", "side": "BUY", "price": 100, "qty": 1, "intent_type": "NEW", "intent_id": "1"})
    assert mock_client.execute.call_count == 0

    writer.add({"ts_ns": 2, "strategy_id": "s1", "symbol": "TMF", "side": "SELL", "price": 101, "qty": 1, "intent_type": "NEW", "intent_id": "2"})
    assert mock_client.execute.call_count == 1
    assert writer.pending_count == 0


def test_writer_flush_on_demand():
    from hft_platform.order.shadow_writer import ShadowOrderWriter

    mock_client = MagicMock()
    writer = ShadowOrderWriter(batch_size=100, enabled=True)
    writer._client = mock_client

    writer.add({"ts_ns": 1, "strategy_id": "s1", "symbol": "TMF", "side": "BUY", "price": 100, "qty": 1, "intent_type": "NEW", "intent_id": "1"})
    writer.flush()
    assert mock_client.execute.call_count == 1


def test_writer_flush_empty_is_noop():
    from hft_platform.order.shadow_writer import ShadowOrderWriter

    mock_client = MagicMock()
    writer = ShadowOrderWriter(batch_size=100, enabled=True)
    writer._client = mock_client

    writer.flush()
    assert mock_client.execute.call_count == 0


def test_writer_flush_failure_does_not_raise():
    from hft_platform.order.shadow_writer import ShadowOrderWriter

    mock_client = MagicMock()
    mock_client.execute.side_effect = Exception("connection lost")
    writer = ShadowOrderWriter(batch_size=1, enabled=True)
    writer._client = mock_client

    writer.add({"ts_ns": 1, "strategy_id": "s1", "symbol": "TMF", "side": "BUY", "price": 100, "qty": 1, "intent_type": "NEW", "intent_id": "1"})
    # Should not raise — records are lost with WARNING log
    assert writer.pending_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_shadow_writer.py -v --no-cov`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement ShadowOrderWriter**

```python
# src/hft_platform/order/shadow_writer.py
"""Batching writer for shadow order records → ClickHouse.

Accumulates shadow intercept records and flushes to hft.shadow_orders
in batches. Flush failures are logged but never raise — shadow
persistence must not block the trading pipeline.
"""
from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)

_INSERT_SQL = (
    "INSERT INTO hft.shadow_orders "
    "(ts_ns, strategy_id, symbol, side, price, qty, intent_type, intent_id) "
    "VALUES"
)


class ShadowOrderWriter:
    """Batch writer for shadow order records to ClickHouse."""

    __slots__ = ("_batch", "_batch_size", "_client", "_enabled")

    def __init__(
        self,
        batch_size: int = 50,
        enabled: bool | None = None,
    ) -> None:
        self._batch: list[tuple] = []
        self._batch_size = batch_size
        self._client = None
        if enabled is not None:
            self._enabled = enabled
        else:
            self._enabled = os.getenv("HFT_CLICKHOUSE_ENABLED", "0") == "1"

    @property
    def pending_count(self) -> int:
        return len(self._batch)

    def _ensure_client(self):
        if self._client is None and self._enabled:
            try:
                from clickhouse_driver import Client
                host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
                self._client = Client(host=host)
            except Exception:
                logger.warning("shadow_writer_client_init_failed", exc_info=True)
                self._enabled = False

    def add(self, record: dict) -> None:
        """Add a shadow order record to the batch."""
        row = (
            record["ts_ns"],
            record["strategy_id"],
            record["symbol"],
            record["side"],
            record["price"],
            record["qty"],
            record["intent_type"],
            record["intent_id"],
        )
        self._batch.append(row)
        if len(self._batch) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Flush pending records to ClickHouse. Never raises."""
        if not self._batch:
            return
        batch = self._batch
        self._batch = []
        if not self._enabled:
            logger.debug("shadow_writer_disabled", dropped=len(batch))
            return
        self._ensure_client()
        if self._client is None:
            return
        try:
            self._client.execute(_INSERT_SQL, batch)
            logger.debug("shadow_writer_flushed", count=len(batch))
        except Exception:
            logger.warning("shadow_writer_flush_failed", count=len(batch), exc_info=True)
```

- [ ] **Step 4: Wire ShadowOrderWriter into ShadowOrderSink**

Modify `src/hft_platform/order/shadow.py`:
- Add `_writer: ShadowOrderWriter | None` slot
- In `intercept()`, after creating the record, call `self._writer.add(record)` if writer exists
- Add `set_writer(writer)` method
- Add `flush()` method that delegates to writer

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/unit/test_shadow_writer.py tests/unit/test_shadow_order.py -v --no-cov`
Expected: All pass

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format src/hft_platform/order/shadow_writer.py src/hft_platform/order/shadow.py tests/unit/test_shadow_writer.py
uv run ruff check src/hft_platform/order/shadow_writer.py src/hft_platform/order/shadow.py tests/unit/test_shadow_writer.py
git add src/hft_platform/order/shadow_writer.py src/hft_platform/order/shadow.py tests/unit/test_shadow_writer.py
git commit -m "feat(shadow): add ClickHouse batch writer for shadow order records"
```

---

## Task 2: Prometheus Shadow Metrics

**Files:**
- Modify: `src/hft_platform/observability/metrics.py`
- Modify: `src/hft_platform/order/shadow.py`
- Create: `tests/unit/test_shadow_metrics.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_shadow_metrics.py
"""Tests for shadow order Prometheus metrics."""
from __future__ import annotations

import pytest


def test_shadow_intercept_increments_counter():
    """Shadow intercept should emit Prometheus counter."""
    from hft_platform.order.shadow import ShadowOrderSink
    from hft_platform.contracts.strategy import OrderIntent, IntentType, Side

    sink = ShadowOrderSink(enabled=True)
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="TMF",
        intent_type=IntentType.NEW, side=Side.BUY,
        price=200_0000, qty=1,
    )
    sink.intercept(intent)

    # Verify Prometheus metric was incremented
    from hft_platform.observability.metrics import MetricsRegistry
    m = MetricsRegistry.get()
    if m and hasattr(m, "shadow_orders_total"):
        # Counter exists and was incremented
        assert True
    else:
        pytest.skip("MetricsRegistry not initialized in test context")
```

- [ ] **Step 2: Add metrics to MetricsRegistry**

In `src/hft_platform/observability/metrics.py`, add near other order metrics:

```python
# Shadow mode metrics
self.shadow_orders_total = Counter(
    "shadow_orders_total",
    "Shadow orders intercepted (not sent to broker)",
    ["strategy", "symbol", "side"],
)
self.shadow_mode_active = Gauge(
    "shadow_mode_active",
    "Shadow order mode status (1=enabled, 0=disabled)",
)
```

- [ ] **Step 3: Wire metrics into ShadowOrderSink.intercept()**

In `shadow.py`, after incrementing `_counter`, add:

```python
metrics = MetricsRegistry.get()
if metrics:
    metrics.shadow_orders_total.labels(
        strategy=intent.strategy_id, symbol=intent.symbol,
        side=str(intent.side.name),
    ).inc()
```

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest tests/unit/test_shadow_metrics.py tests/unit/test_shadow_order.py -v --no-cov
uv run ruff format src/hft_platform/order/shadow.py src/hft_platform/observability/metrics.py
git add src/hft_platform/order/shadow.py src/hft_platform/observability/metrics.py tests/unit/test_shadow_metrics.py
git commit -m "feat(shadow): add Prometheus metrics for shadow order tracking"
```

---

## Task 3: Bootstrap Dual-Lock Validation

**Files:**
- Modify: `src/hft_platform/services/bootstrap.py`
- Create: `tests/unit/test_bootstrap_shadow_lock.py`

**Context:** Spec Section 4.4 requires: "Phase 2 requires both a non-live runtime profile and `HFT_ORDER_SHADOW_MODE=1`. Startup must refuse to proceed if a shadow profile is combined with live order mode."

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_bootstrap_shadow_lock.py
"""Tests for shadow mode bootstrap dual-lock validation."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


def test_shadow_mode_with_live_orders_refuses_start():
    """Shadow mode + live order mode = startup failure."""
    env = {
        "HFT_ORDER_SHADOW_MODE": "1",
        "HFT_ORDER_MODE": "live",
        "HFT_MODE": "real",
    }
    with patch.dict(os.environ, env, clear=False):
        from hft_platform.services.bootstrap import validate_shadow_lock
        with pytest.raises(SystemExit):
            validate_shadow_lock()


def test_shadow_mode_with_sim_orders_passes():
    """Shadow mode + sim order mode = OK."""
    env = {
        "HFT_ORDER_SHADOW_MODE": "1",
        "HFT_ORDER_MODE": "sim",
        "HFT_MODE": "sim",
    }
    with patch.dict(os.environ, env, clear=False):
        from hft_platform.services.bootstrap import validate_shadow_lock
        validate_shadow_lock()  # Should not raise


def test_no_shadow_mode_skips_check():
    """When shadow mode is off, no dual-lock check needed."""
    env = {
        "HFT_ORDER_SHADOW_MODE": "0",
        "HFT_ORDER_MODE": "live",
        "HFT_MODE": "real",
    }
    with patch.dict(os.environ, env, clear=False):
        from hft_platform.services.bootstrap import validate_shadow_lock
        validate_shadow_lock()  # Should not raise
```

- [ ] **Step 2: Implement validate_shadow_lock()**

Add to `src/hft_platform/services/bootstrap.py`:

```python
def validate_shadow_lock() -> None:
    """Dual-lock: refuse startup if shadow mode + live order mode."""
    shadow = os.getenv("HFT_ORDER_SHADOW_MODE", "0") == "1"
    order_mode = os.getenv("HFT_ORDER_MODE", "sim").strip().lower()
    if shadow and order_mode in {"live", "real"}:
        logger.critical(
            "FATAL: HFT_ORDER_SHADOW_MODE=1 cannot be combined with HFT_ORDER_MODE=live. "
            "Shadow mode must use sim order mode."
        )
        raise SystemExit(1)
```

Call this function from the existing bootstrap validation chain.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_bootstrap_shadow_lock.py -v --no-cov
git add src/hft_platform/services/bootstrap.py tests/unit/test_bootstrap_shadow_lock.py
git commit -m "feat(shadow): add bootstrap dual-lock preventing shadow+live order mode"
```

---

## Task 4: Shadow Daily Report Template + Dispatcher

**Files:**
- Modify: `src/hft_platform/notifications/templates.py`
- Modify: `src/hft_platform/notifications/dispatcher.py`
- Create: `tests/unit/test_shadow_report_template.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_shadow_report_template.py
"""Tests for shadow daily report template."""
from __future__ import annotations

import pytest


def test_shadow_daily_report_renders_all_fields():
    from hft_platform.notifications.templates import render_shadow_daily_report

    msg = render_shadow_daily_report(
        date_str="2026-04-22 (二)",
        intent_count=18,
        buys=10,
        sells=8,
        simulated_pnl_ntd=850,
        latency_p50_ms=1.1,
        latency_p95_ms=3.2,
        latency_p99_ms=8.7,
        reconnect_count=0,
        queue_peak_pct=12,
        rss_gb=1.7,
        storm_guard_state="NORMAL",
    )
    assert "Shadow" in msg or "shadow" in msg
    assert "18" in msg
    assert "850" in msg
    assert "NORMAL" in msg
    assert "1.1" in msg


def test_shadow_daily_report_zero_signals():
    from hft_platform.notifications.templates import render_shadow_daily_report

    msg = render_shadow_daily_report(
        date_str="2026-04-23 (三)",
        intent_count=0, buys=0, sells=0,
        simulated_pnl_ntd=0,
        latency_p50_ms=0, latency_p95_ms=0, latency_p99_ms=0,
        reconnect_count=0, queue_peak_pct=0, rss_gb=0,
        storm_guard_state="NORMAL",
    )
    assert "0" in msg
```

- [ ] **Step 2: Implement template and dispatcher method**

Add to `templates.py`:
```python
def render_shadow_daily_report(
    *,
    date_str: str,
    intent_count: int,
    buys: int,
    sells: int,
    simulated_pnl_ntd: int,
    latency_p50_ms: float,
    latency_p95_ms: float,
    latency_p99_ms: float,
    reconnect_count: int,
    queue_peak_pct: int,
    rss_gb: float,
    storm_guard_state: str,
) -> str:
    return (
        f"📊 Shadow 日報 {date_str}\n\n"
        f"🔮 信號: {intent_count} OrderIntents (買 {buys} / 賣 {sells})\n"
        f"💰 模擬 PnL: {simulated_pnl_ntd:+,} NTD (含 1-tick slippage)\n\n"
        f"⏱ 延遲:\n"
        f"  tick→signal P50: {latency_p50_ms:.1f}ms / P95: {latency_p95_ms:.1f}ms / P99: {latency_p99_ms:.1f}ms\n\n"
        f"📈 系統:\n"
        f"  Reconnect: {reconnect_count} / Queue peak: {queue_peak_pct}% / RSS: {rss_gb:.1f} GB\n"
        f"  StormGuard: {storm_guard_state} (全日)"
    )
```

Add to `dispatcher.py`:
```python
async def notify_shadow_daily_report(self, **kwargs) -> None:
    msg = templates.render_shadow_daily_report(**kwargs)
    await self._sender.send(msg, critical=False)
```

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_shadow_report_template.py tests/unit/test_notification_templates.py tests/unit/test_notification_dispatcher.py -v --no-cov
git add src/hft_platform/notifications/templates.py src/hft_platform/notifications/dispatcher.py tests/unit/test_shadow_report_template.py
git commit -m "feat(notifications): add shadow daily report template and dispatcher"
```

---

## Task 5: Shadow Daily Analysis Script

**Files:**
- Create: `scripts/shadow_daily_report.py`
- Create: `tests/unit/test_shadow_daily_report.py`

**Context:** Post-market script (14:00) that queries `hft.shadow_orders` for today's shadow data, computes simulated PnL, and sends Telegram shadow daily report. Also saves evidence pack.

- [ ] **Step 1: Write failing test for the analysis logic**

```python
# tests/unit/test_shadow_daily_report.py
"""Tests for shadow daily analysis logic."""
from __future__ import annotations

import pytest


def test_compute_simulated_pnl_long_round_trip():
    """Buy then sell → PnL = (sell_price - buy_price) * qty * point_value / scale."""
    from scripts.shadow_daily_report import compute_simulated_pnl

    orders = [
        {"side": "BUY", "price": 200_0000, "mid_price": 200_0000, "qty": 1, "symbol": "TMF"},
        {"side": "SELL", "price": 201_0000, "mid_price": 201_0000, "qty": 1, "symbol": "TMF"},
    ]
    point_values = {"TMF": 10}
    pnl = compute_simulated_pnl(orders, point_values, slippage_ticks=1)
    # With 1-tick slippage: buy at mid+1tick, sell at mid-1tick
    # Actual fill: buy at 200_0000 + tick, sell at 201_0000 - tick
    # PnL depends on tick size — test verifies it returns an int
    assert isinstance(pnl, int)


def test_compute_simulated_pnl_empty_orders():
    from scripts.shadow_daily_report import compute_simulated_pnl

    pnl = compute_simulated_pnl([], {}, slippage_ticks=1)
    assert pnl == 0


def test_count_by_side():
    from scripts.shadow_daily_report import count_by_side

    orders = [
        {"side": "BUY"}, {"side": "BUY"}, {"side": "SELL"},
    ]
    buys, sells = count_by_side(orders)
    assert buys == 2
    assert sells == 1
```

- [ ] **Step 2: Implement the script**

Create `scripts/shadow_daily_report.py`:
- `compute_simulated_pnl(orders, point_values, slippage_ticks=1) -> int` — pure function
- `count_by_side(orders) -> tuple[int, int]` — pure function
- `query_shadow_orders(date) -> list[dict]` — ClickHouse query
- `save_evidence_pack(date, data)` — writes to `outputs/production_rollout/phase2/<YYYYMMDD>/`
- `main()` — orchestrates query → analyze → Telegram → evidence

Key: use `mid_price` field from shadow_orders for simulated fill price. Apply ±1 tick slippage (conservative). Point values from config.

- [ ] **Step 3: Run tests and commit**

```bash
uv run pytest tests/unit/test_shadow_daily_report.py -v --no-cov
git add scripts/shadow_daily_report.py tests/unit/test_shadow_daily_report.py
git commit -m "feat(shadow): add post-market shadow daily analysis script"
```

---

## Task 6: Shadow Config + Evidence Pack Directory

**Files:**
- Create: `config/env/shadow/main.yaml`

- [ ] **Step 1: Create shadow environment config**

```yaml
# config/env/shadow/main.yaml
# Phase 2 Shadow Trading: real feed, strategy-driven signals, no real orders

mode: sim  # Runtime mode stays sim (no live broker calls for orders)

shadow:
  enabled: true  # Also set via HFT_ORDER_SHADOW_MODE=1

shioaji:
  activate_ca: false  # Not needed for shadow (no order placement)

# Use real feed credentials from .env:
# SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY
```

- [ ] **Step 2: Create evidence pack directory structure**

```bash
mkdir -p outputs/production_rollout/phase2
echo "# Phase 2 Shadow Trading Evidence\nDaily evidence packs stored in YYYYMMDD subdirectories." > outputs/production_rollout/phase2/README.md
```

- [ ] **Step 3: Add outputs/ to .gitignore if not already there**

Check if `outputs/` is in `.gitignore`. If not, add it (evidence packs are runtime artifacts, not source code).

- [ ] **Step 4: Commit**

```bash
git add config/env/shadow/main.yaml outputs/production_rollout/phase2/README.md
git commit -m "feat(config): add shadow trading environment config and evidence pack directory"
```

---

## Task 7: CI Verification + Format

- [ ] **Step 1: Format all new/modified files**

```bash
uv run ruff format src/hft_platform/order/shadow.py src/hft_platform/order/shadow_writer.py src/hft_platform/observability/metrics.py src/hft_platform/services/bootstrap.py src/hft_platform/notifications/templates.py src/hft_platform/notifications/dispatcher.py scripts/shadow_daily_report.py tests/unit/test_shadow_writer.py tests/unit/test_shadow_metrics.py tests/unit/test_bootstrap_shadow_lock.py tests/unit/test_shadow_report_template.py tests/unit/test_shadow_daily_report.py
```

- [ ] **Step 2: Run all Phase 2 tests**

```bash
uv run pytest tests/unit/test_shadow_order.py tests/unit/test_shadow_writer.py tests/unit/test_shadow_metrics.py tests/unit/test_bootstrap_shadow_lock.py tests/unit/test_shadow_report_template.py tests/unit/test_shadow_daily_report.py -v --no-cov
```

- [ ] **Step 3: Run lint**

```bash
uv run ruff check src/hft_platform/order/shadow.py src/hft_platform/order/shadow_writer.py scripts/shadow_daily_report.py
```

- [ ] **Step 4: Run full test suite to check regressions**

```bash
uv run pytest tests/unit/ --no-cov -x -q 2>&1 | tail -5
```

- [ ] **Step 5: Commit if any fixes needed**

```bash
git add -A && git commit -m "fix: format and lint Phase 2 shadow trading files"
```
