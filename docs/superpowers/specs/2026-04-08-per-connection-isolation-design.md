# Per-Connection Isolation for QuoteConnectionPool

**Date**: 2026-04-08
**Status**: Approved
**Scope**: Data + Infra plane (QuoteConnectionPool, StormGuard wiring, LOB/Feature reset)

## Problem

The platform runs 4 quote connections via `QuoteConnectionPool`, each handling ~200 symbols. The pool duck-types as a single connection, which causes cascading failures:

1. **Single-connection drop triggers platform-wide STORM** — `get_max_feed_gap_s()` returns the global maximum across all symbols. One disconnected facade's stale symbols dominate the max, triggering StormGuard STORM for all 1000 symbols.
2. **Reconnect storm** — `pool.reconnect()` reconnects ALL facades sequentially. One failing facade causes healthy facades to be torn down and reconnected unnecessarily.
3. **Sequential timeout exhaustion** — 4 facades × ~8s each = ~32s, exceeding the 30s timeout, marking everything DISCONNECTED.
4. **Global state reset** — LOB books and FeatureEngine state are reset for ALL symbols on any reconnect, even if only one facade was affected.

These bugs have caused production trading halts.

## Solution: Pool-Internal State Machine (Approach A)

Add per-facade state tracking inside `QuoteConnectionPool`. Each facade gets an independent lifecycle with targeted reconnect and warmup reset. StormGuard only considers healthy facades for feed gap calculation.

## Design

### 1. FacadeSlot Data Structure

```python
class FacadeState(IntEnum):
    CONNECTED = 0      # Normal — data flowing
    DEGRADED = 1       # Data gap detected, grace period active
    RECOVERING = 2     # Reconnect in progress
    DISCONNECTED = 3   # Reconnect failed, awaiting retry with backoff

class FacadeSlot:
    __slots__ = (
        "conn_id", "facade", "state", "symbols",
        "last_data_mono", "last_reconnect_mono",
        "reconnect_failures", "degraded_since_mono",
    )
```

### 2. State Transitions

```
CONNECTED ──[gap > 3s]──────────→ DEGRADED
DEGRADED  ──[data resumes]──────→ CONNECTED
DEGRADED  ──[gap > 10s]─────────→ RECOVERING
RECOVERING ──[reconnect ok]─────→ CONNECTED (+ warmup reset)
RECOVERING ──[reconnect fail]───→ DISCONNECTED
DISCONNECTED ──[backoff timer]──→ RECOVERING
```

- **DEGRADED grace period** (3s default, `HFT_FACADE_DEGRADED_THRESHOLD_S`): Prevents transient jitter from triggering reconnect.
- **Reconnect trigger** (10s default, `HFT_FACADE_RECONNECT_TRIGGER_S`): Time in DEGRADED before initiating reconnect.
- **Backoff**: `min(120, 5 * 2^reconnect_failures)` seconds per-facade.

### 3. Per-Facade Reconnect

```python
async def reconnect_facade(self, conn_id: int, reason: str = "") -> bool:
```

- Reconnects a **single** facade with its own timeout (`HFT_PER_FACADE_TIMEOUT_S`, default 15s).
- On success: state → CONNECTED, reset failures, call `_notify_warmup_reset(conn_id)`.
- On failure: state → DISCONNECTED, increment `reconnect_failures`.
- Multiple facades needing reconnect are handled via `asyncio.gather` (parallel).

The existing `reconnect()` duck-type method changes to only reconnect non-CONNECTED facades, skipping healthy ones. Returns `True` if at least one facade is CONNECTED.

### 4. Health Check Integration

`check_facade_health()` is called from the existing `system.py` monitor loop (~200ms interval). No new thread or task.

```python
def check_facade_health(self) -> None:
    now = time.monotonic()
    for slot in self._slots:
        gap = now - slot.last_data_mono
        # CONNECTED → DEGRADED
        if slot.state == FacadeState.CONNECTED and gap > self._degraded_threshold_s:
            slot.state = FacadeState.DEGRADED
            slot.degraded_since_mono = now
        # DEGRADED → CONNECTED (recovery) or → RECOVERING (trigger reconnect)
        elif slot.state == FacadeState.DEGRADED:
            if gap <= self._recovery_threshold_s:
                slot.state = FacadeState.CONNECTED
            elif now - slot.degraded_since_mono > self._reconnect_trigger_s:
                self._schedule_reconnect(slot.conn_id)
        # DISCONNECTED → RECOVERING (retry with backoff)
        elif slot.state == FacadeState.DISCONNECTED:
            backoff = min(120, 5 * (2 ** slot.reconnect_failures))
            if now - slot.last_reconnect_mono > backoff:
                self._schedule_reconnect(slot.conn_id)
```

### 5. StormGuard Feed Gap Isolation

Pool exposes `get_healthy_feed_gap_s()`:

```python
def get_healthy_feed_gap_s(self) -> float:
    now = time.monotonic()
    max_gap = 0.0
    has_connected = False
    for slot in self._slots:
        if slot.state == FacadeState.CONNECTED:
            has_connected = True
            gap = now - slot.last_data_mono
            if gap > max_gap:
                max_gap = gap
    if not has_connected:
        return float("inf")  # All facades down → trigger HALT
    return max_gap
```

`system.py` changes `_get_max_feed_gap_s()` to call this when the client is a `QuoteConnectionPool`.

**Behavior matrix**:

| Scenario | Before | After |
|----------|--------|-------|
| 1/4 connections drops | STORM (all symbols) | Only affected symbols DEGRADED; others trade normally |
| 1/4 drops > 10s | STORM continues | Affected facade auto-reconnects; others unaffected |
| 4/4 connections drop | STORM → HALT | `inf` gap → HALT (correct safety net preserved) |

### 6. Callback `last_data_mono` Update

Each facade's callback is wrapped at `subscribe_all` time to update the slot's `last_data_mono`:

```python
def _make_callback_wrapper(slot, original_cb):
    def wrapper(*args, **kwargs):
        slot.last_data_mono = time.monotonic()
        original_cb(*args, **kwargs)
    return wrapper
```

Cost: ~50ns per callback (`time.monotonic()` + `STORE_ATTR`). Thread-safe under CPython GIL for float assignment.

### 7. Targeted Warmup Reset

On successful per-facade reconnect, `_notify_warmup_reset(conn_id)` calls:

- `lob.reset_books_for_symbols(slot.symbols)` — new method, resets only affected symbols
- `fe.reset_symbols(slot.symbols)` — new method, delegates to existing `reset_symbol()` per symbol

Pool receives `lob` and `fe` references via injection at bootstrap time.

**Strategy impact**: None. `FeatureUpdateEvent.warmup_ready_mask` automatically reflects the reset state. Strategies checking `warmup_ready` skip immature features without code changes.

### 8. `_md_reconnect.py` Changes

- Remove the unconditional `lob.reset_books()` + `fe.reset_all()` from `_trigger_reconnect` (lines 163-168). Pool handles this via `_notify_warmup_reset`.
- `_trigger_reconnect` return value: `True` if at least one facade is CONNECTED (not all-or-nothing).

### 9. Options Refresh Thread Coordination

`_refresh_lock` scope is extended to check slot state before operating on a facade. If a slot is in RECOVERING, the refresh skips that facade to avoid concurrent SDK operations.

### 10. Per-Facade Metrics

```
hft_quote_conn_state           {conn_id}   # FacadeState enum value
hft_quote_conn_feed_gap_s      {conn_id}   # Current per-facade feed gap
hft_quote_conn_reconnect_total {conn_id, result}  # ok/fail/timeout counts
```

4 conn_ids × small label set = bounded cardinality.

## File Change Summary

| File | Changes | Lines |
|------|---------|-------|
| `feed_adapter/shioaji/quote_connection_pool.py` | `FacadeState`, `FacadeSlot`, `reconnect_facade()`, `check_facade_health()`, `get_healthy_feed_gap_s()`, callback wrapper, `_notify_warmup_reset()`. Rewrite `reconnect()` to skip healthy facades. | +180 |
| `services/system.py` | StormGuard wiring uses `get_healthy_feed_gap_s()`, monitor loop calls `check_facade_health()` | +15, -5 |
| `services/_md_reconnect.py` | Remove global LOB/Feature reset, adjust return semantics | +5, -10 |
| `feed_adapter/lob_engine.py` | New `reset_books_for_symbols(symbols)` | +8 |
| `feature/engine.py` | New `reset_symbols(symbols)` | +5 |
| `services/bootstrap.py` | Inject `lob` + `fe` into Pool | +3 |
| **Total** | | **~220 lines** |

## What Does NOT Change

- `MarketDataService` core (raw_queue processing, normalizer, event dispatch)
- `StrategyRunner` (warmup works via existing `warmup_ready_mask`)
- `OrderAdapter` (order tracking scoping is out of scope)
- `StormGuard` internals (only the `feed_gap_s` input value changes)
- Existing duck-type interface signatures (`reconnect()`, `resubscribe()`, `subscribe_basket()`)

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_FACADE_DEGRADED_THRESHOLD_S` | `3` | Gap before CONNECTED → DEGRADED |
| `HFT_FACADE_RECONNECT_TRIGGER_S` | `10` | Time in DEGRADED before triggering reconnect |
| `HFT_PER_FACADE_TIMEOUT_S` | `15` | Per-facade reconnect timeout |

## Testing Strategy

1. **Unit**: FacadeSlot state transitions, `get_healthy_feed_gap_s()` with mixed states
2. **Integration**: Mock 4 facades, simulate single-facade drop, verify StormGuard stays NORMAL
3. **Regression**: Verify all-facade drop still triggers HALT
4. **Manual**: Replay production reconnect logs against new logic
