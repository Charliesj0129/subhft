# Pipeline Determinism & Async Defense — Design Spec

**Date**: 2026-03-27
**Scope**: 6.1 Internal Pipeline Execution Determinism + 6.2 Async Concurrency & State Machine Defense
**Status**: Draft
**Estimated LOC**: ~170 lines of changes + unit tests
**Rev**: 2 (addresses code review findings 1-5)

---

## 1. Problem Statement

Audit of the HFT platform's execution pipeline identified 8 correctness defects across queue safety, state machine defense, and timing guarantees. These defects can cause:

- **Silent fill loss** → position drift, incorrect PnL, StormGuard failing to trigger
- **Orphan orders** → live_orders dict grows unbounded, cancel operations miss targets
- **Non-monotonic timeouts** → NTP slew causes deadlines to expire early or cooldowns to never end
- **Post-HALT order injection** → blocking await unblocks after supervisor drain
- **Partial intent batches** → multi-leg strategies submit only half their orders
- **Session gate bypass** → unregistered symbols trade through all session phases

None of these are theoretical: they are reachable under production conditions (queue saturation, broker callback timing, NTP adjustment, new symbol onboarding).

---

## 2. Defect Inventory

| ID | Severity | Component | Defect |
|----|----------|-----------|--------|
| D1 | **High** | `system.py:679` | `raw_exec_queue` full → broker fills silently dropped via `call_soon_threadsafe(put_nowait)` |
| D2 | **High** | `adapter.py:636` | `live_orders` written after `place_order` returns → terminal callback arrives before registration |
| D3 | **High** | Multiple (8 sites) | `timebase.now_ns()`/`now_s()` wall-clock used for timeout/cooldown calculations |
| D4 | **Medium** | `risk/engine.py` | Standalone RiskEngine `await order_queue.put()` unblocks after supervisor HALT drain |
| D5 | **Medium** | `runner.py:462` | `PositionStore.positions` snapshot via `dict(raw)` without `_fill_lock` |
| D6 | **Medium** | `session_governor.py:91` | Unknown symbols default to `SessionPhase.OPEN` — bypass all session gating |
| D7 | **Medium** | `runner.py:760-786` | `QueueFull` in intent submit loop crashes runner; partial batch silently submitted |
| D8 | **Low** | `storm_guard.py:281` | `loop.create_task(result)` on halt callback — exception silently swallowed |

---

## 3. Design

### D1: Exec Queue Overflow Ring Buffer

**Root cause**: `loop.call_soon_threadsafe(self.raw_exec_queue.put_nowait, event)` — when queue is full, `QueueFull` is raised inside the event loop callback with no caller to catch it.

**Fix**:

#### 3.1.1 New method `HFTSystem._safe_enqueue_exec`

```python
def _safe_enqueue_exec(self, event: RawExecEvent) -> None:
    try:
        self.raw_exec_queue.put_nowait(event)
    except asyncio.QueueFull:
        buf_len = len(self._exec_overflow_buf)
        if buf_len >= self._EXEC_OVERFLOW_MAX:
            # Overflow buffer itself is full — HALT immediately and log the lost fill
            self._exec_overflow_evicted += 1
            self.metrics.exec_overflow_evicted_total.inc()
            logger.critical("exec_overflow_buf FULL — fill LOST",
                            evicted_count=self._exec_overflow_evicted,
                            event_topic=getattr(event, 'topic', '?'))
            self.storm_guard.trigger_halt("exec_overflow_buf_exhausted")
            return  # explicitly drop — do NOT silently evict oldest
        self._exec_overflow_buf.append(event)
        self._exec_overflow_counter += 1
        self.metrics.exec_queue_overflow_total.inc()
        logger.critical("raw_exec_queue FULL — fill routed to overflow buffer",
                        overflow_count=self._exec_overflow_counter,
                        buf_depth=buf_len + 1)
        if self._exec_overflow_counter >= 3:
            self.storm_guard.trigger_halt("exec_queue_overflow_repeated")
```

#### 3.1.2 `_on_exec` uses wrapper

```python
loop.call_soon_threadsafe(self._safe_enqueue_exec, event)
```

#### 3.1.3 Overflow buffer

- `self._exec_overflow_buf: collections.deque[RawExecEvent] = collections.deque()` — **unbounded** deque (no `maxlen`).
- `self._EXEC_OVERFLOW_MAX: int = 4096` — explicit capacity check in `_safe_enqueue_exec`.
- `self._exec_overflow_counter: int = 0` — cumulative overflow count (never reset).
- `self._exec_overflow_evicted: int = 0` — cumulative count of fills lost after buffer exhaustion.

**Why unbounded deque with explicit check instead of `deque(maxlen=N)`**: A `deque(maxlen=N)` silently evicts the oldest element on append when full. This converts one silent-loss path into another. Instead, we use an unbounded deque with an explicit length check before append. When the buffer is full, we refuse to append, log the lost fill with CRITICAL, increment `exec_overflow_evicted_total`, and trigger immediate StormGuard HALT. The fill is still lost, but:
1. The loss is **explicit** — logged with event details, counted in a dedicated metric.
2. HALT is triggered **immediately** (not after 3 overflows) — the system is in catastrophic backpressure.
3. No older fills are silently evicted — the buffer preserves all fills it accepted.

#### 3.1.4 ExecutionRouter drains overflow first

`HFTSystem` passes `_exec_overflow_buf` reference to `ExecutionRouter` at construction. Router's `run()` loop, after each `await raw_queue.get()`, drains the overflow buffer:

```python
while self._overflow_buf:
    overflow_event = self._overflow_buf.popleft()
    self._process_event(overflow_event)
    self.metrics.exec_overflow_drained_total.inc()
```

#### 3.1.5 Observability

- `exec_queue_overflow_total` — Counter: fills routed to overflow buffer
- `exec_overflow_drained_total` — Counter: fills successfully drained from overflow
- `exec_overflow_evicted_total` — Counter: fills **lost** when overflow buffer is also full
- CRITICAL log per overflow event (with buffer depth)
- CRITICAL log per evicted fill (with event topic for forensics)
- StormGuard HALT after 3 cumulative overflows OR immediately on buffer exhaustion

---

### D2: OrderAdapter live_orders TOCTOU Fix

**Root cause**: `live_orders[order_key] = trade` at `adapter.py:636` executes after `place_order` returns. A broker terminal callback arriving in this window cannot resolve the order because:
1. `live_orders` has no entry yet (keyed by `"{strategy_id}:{intent_id}"`).
2. `order_id_map` has no entry yet (`_register_broker_ids` hasn't run).
3. `on_terminal_state` receives `(strategy_id, broker_order_id)`, resolves via `OrderIdResolver.resolve_order_key()` which looks up `order_id_map[broker_order_id]` — misses → orphan order.

The race window is: `place_order()` returns → `_register_broker_ids()` completes. Pre-registering in `live_orders` alone (original design) does not help because the terminal callback resolves via `order_id_map`, not by direct `live_orders` key lookup.

**Fix**:

#### 3.2.1 Sentinel objects (module-level)

```python
_PENDING_SENTINEL = object()
_TERMINAL_BEFORE_REGISTERED = object()
```

#### 3.2.2 Pre-register in BOTH `live_orders` and `order_id_map` before API call

The broker `order_id` (e.g., `ord_no`) is not known until `place_order` returns. Therefore we cannot pre-register the broker→key mapping. Instead, we register the **reverse direction**: record that `order_key` is in-flight, and handle unresolvable terminal callbacks via a pending-terminal queue.

```python
# _dispatch_to_api, NEW path
order_key = f"{intent.strategy_id}:{intent.intent_id}"
async with self._live_orders_lock:
    self.live_orders[order_key] = _PENDING_SENTINEL
    self._pending_order_keys.add(order_key)  # track in-flight orders

trade = await self._call_api("place_order", ...)

if trade is None:
    async with self._live_orders_lock:
        self.live_orders.pop(order_key, None)
        self._pending_order_keys.discard(order_key)
    return
```

#### 3.2.3 `_register_broker_ids` + deferred terminal drain

After `place_order` succeeds, register broker IDs as before, then drain any terminal callbacks that arrived during the race window:

```python
# After successful place_order:
async with self._live_orders_lock:
    self.live_orders[order_key] = trade
    self._pending_order_keys.discard(order_key)

await self._register_broker_ids(order_key, trade)

# Drain deferred terminals that arrived before registration
await self._drain_deferred_terminals(order_key, trade)
```

#### 3.2.4 `on_terminal_state` — deferred terminal queue for unresolvable callbacks

The resolver chain in `OrderIdResolver.resolve_order_key()` falls through to a constructed key `"{strategy_id}:{broker_order_id}"` when `order_id_map` has no entry. This constructed key will NOT match the `live_orders` sentinel key (`"{strategy_id}:{intent_id}"`). We detect this case:

```python
async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
    async with self._live_orders_lock:
        order_key = self.order_id_resolver.resolve_order_key(
            strategy_id, order_id, self.live_orders)
        entry = self.live_orders.get(order_key)

        if entry is not None and entry is not _PENDING_SENTINEL:
            # Normal path — order is registered, clean up
            del self.live_orders[order_key]
            return

        # Check if ANY order for this strategy_id is in-flight (pending sentinel)
        has_pending = any(
            k.startswith(f"{strategy_id}:") for k in self._pending_order_keys
        )
        if has_pending:
            # Terminal arrived before _register_broker_ids — defer for later drain
            self._deferred_terminals.append((strategy_id, order_id, time.monotonic()))
            self.metrics.terminal_before_registration_total.inc()
            logger.warning("terminal_before_registration",
                           strategy_id=strategy_id, broker_order_id=order_id)
            return

        # No pending orders — this is a genuine orphan (or already cleaned up)
        if order_key in self.live_orders:
            del self.live_orders[order_key]
```

#### 3.2.5 `_drain_deferred_terminals`

Called after `_register_broker_ids` completes (broker IDs are now in `order_id_map`):

```python
async def _drain_deferred_terminals(self, order_key: str, trade: Any) -> None:
    """Re-process deferred terminal callbacks now that broker IDs are registered."""
    remaining: list[tuple[str, str, float]] = []
    async with self._live_orders_lock:
        for sid, oid, ts in self._deferred_terminals:
            resolved = self.order_id_resolver.resolve_order_key(
                sid, oid, self.live_orders)
            if resolved in self.live_orders:
                del self.live_orders[resolved]
                logger.info("deferred_terminal_cleanup",
                            key=resolved, broker_order_id=oid,
                            defer_age_ms=int((time.monotonic() - ts) * 1000))
            else:
                remaining.append((sid, oid, ts))
        self._deferred_terminals = remaining
```

#### 3.2.6 Init — new fields

```python
self._pending_order_keys: set[str] = set()        # order_keys with in-flight place_order
self._deferred_terminals: list[tuple[str, str, float]] = []  # (strategy_id, broker_order_id, monotonic_ts)
```

#### 3.2.7 Deferred terminal expiry

Stale deferred terminals (older than 30s) are garbage-collected in `_drain_deferred_terminals` to prevent unbounded growth if a terminal never resolves:

```python
# In _drain_deferred_terminals, after the main drain loop:
now = time.monotonic()
remaining = [(s, o, t) for s, o, t in remaining if now - t < 30.0]
for s, o, t in self._deferred_terminals:
    if now - t >= 30.0:
        logger.error("deferred_terminal_expired",
                     strategy_id=s, broker_order_id=o,
                     age_s=round(now - t, 1))
        self.metrics.deferred_terminal_expired_total.inc()
```

#### 3.2.8 Fix `_pending_close_qty` lockless read

`adapter.py:447-455` reads `self.live_orders.values()` without lock. Wrap in `async with self._live_orders_lock`.

---

### D3: Surgical Wall-Clock to Monotonic Migration

**Root cause**: `timebase.now_ns()` / `now_s()` use `time.time_ns()` / `time.time()` (wall-clock). NTP adjustments make these non-monotonic, breaking timeout/cooldown logic.

**Fix — 8 surgical replacements**:

| Site | File | Current | Replacement |
|------|------|---------|-------------|
| 3a | `storm_guard.py` L154-157 | `self._storm_entry_ts = timebase.now_s()` | `time.monotonic()` |
| 3b | `storm_guard.py` L161-166 | `now - self._halt_entry_ts` cooldown | `now = time.monotonic()` |
| 3c | `storm_guard.py` L251 | `self.last_state_change = timebase.now_s()` | `time.monotonic()` |
| 3d | `order/circuit_breaker.py` | `open_until = timebase.now_s() + timeout_s` | `time.monotonic() + timeout_s` |
| 3e | `runner.py` L658 | `_circuit_halted_at_ns[sid] = timebase.now_ns()` | `time.monotonic_ns()` |
| 3f | `runner.py` L538 | `timebase.now_ns() - halted_at >= cooldown_ns` | `time.monotonic_ns()` |
| 3g | `risk/engine.py` | `deadline_ns = timebase.now_ns() + ttl` | `time.monotonic_ns() + ttl` |
| 3h | `order/adapter.py` | `timebase.now_ns() > cmd.deadline_ns` | `time.monotonic_ns()` |

**Not changed** (correct wall-clock usage):
- `RawExecEvent` timestamps, `TickEvent.ts_ns`, recorder/ClickHouse writes, audit logs.

**Type changes**: Field types remain `float` / `int` — only the semantic domain changes from epoch to monotonic. No external consumers depend on epoch semantics for these internal timeout fields.

---

### D4: RiskEngine HALT Drain Race

**Root cause**: `RiskEngine.run()` uses blocking `await order_queue.put(cmd)`. Supervisor's HALT drain of `order_queue` can complete while RiskEngine has an in-flight `put()` waiting for space — which unblocks after drain, injecting a post-HALT order.

**Fix**:

#### 3.4.1 Pre-dispatch HALT guard

```python
# risk/engine.py — after evaluate(), before dispatching
if self.storm_guard.state == StormGuardState.HALT:
    logger.warning("risk_engine_blocked_by_halt", cmd_id=cmd.cmd_id)
    self.metrics.risk_halt_blocked_total.inc()
    return
```

#### 3.4.2 Replace blocking put with put_nowait + DLQ + strategy feedback

```python
try:
    self.order_queue.put_nowait(cmd)
except asyncio.QueueFull:
    # Approved command cannot be dispatched — this is fail-closed, not silent drop
    logger.error("order_queue_full_in_risk",
                 cmd_id=cmd.cmd_id,
                 strategy_id=cmd.intent.strategy_id,
                 symbol=cmd.intent.symbol)
    self.metrics.order_queue_full_total.inc()
    # Route to DLQ for forensics (same pattern as intent channel DLQ)
    self._order_dlq.append((cmd, time.monotonic_ns()))
    if len(self._order_dlq) > self._ORDER_DLQ_MAX:
        self._order_dlq.popleft()  # bounded, oldest evicted
    # Trigger StormGuard escalation — sustained queue saturation is abnormal
    self.storm_guard.trigger_halt("order_queue_full")
```

**Init — new fields on RiskEngine:**
```python
self._order_dlq: collections.deque = collections.deque()  # (OrderCommand, ts_ns) pairs
self._ORDER_DLQ_MAX: int = 256
```

**Rationale**: An already-approved `OrderCommand` that cannot be dispatched is a system-level failure (the order pipeline is backed up). Silent drop is wrong — the command was risk-approved but never reached the broker. The fix:
1. **Logs** the drop with full context (strategy, symbol, cmd_id).
2. **Routes to DLQ** for post-session forensic review (bounded deque, max 256 entries).
3. **Triggers StormGuard HALT** — if the order queue is full, the system is in distress. HALT prevents further order generation while the backlog clears.

This eliminates the blocking window entirely. Either the put succeeds immediately, or it fails with explicit handling. No in-flight await can survive a drain.

**GatewayService mode**: Already uses `put_nowait` with `sg_state` check — unaffected.

---

### D5: PositionStore Atomic Snapshot

**Root cause**: `runner.py:462` calls `dict(raw)` on `PositionStore.positions` without holding `_fill_lock`. Concurrent `on_fill` in thread-pool worker can mutate the dict mid-copy.

**Fix**:

#### 3.5.1 New method on PositionStore

```python
def snapshot_positions(self) -> dict:
    """Return a consistent shallow copy under fill lock."""
    with self._fill_lock:
        return dict(self.positions)
```

#### 3.5.2 StrategyRunner uses snapshot

```python
# runner.py:460-462 — replace direct dict access
raw = self.position_store.snapshot_positions()
```

**Latency impact**: `threading.Lock` acquire + `dict()` copy for 10-50 entries < 1us. Well under the 1ms async law threshold.

**Rust fast-path**: Unaffected — `rust_tracker.get_positions_by_strategy()` already returns an atomic snapshot from Rust side (tried first at line 453-456).

---

### D6: TrackGate Unknown Symbol Default to CLOSED + Per-Intent Gating

**Root cause (two layers)**:

1. **TrackGate layer**: `session_governor.py:91-94` returns `SessionPhase.OPEN` for unregistered symbols — all intents pass through unfiltered.
2. **StrategyRunner layer**: `runner.py:688` gates the entire intent batch using `event.symbol` (the triggering market data event's symbol), NOT each `intent.symbol`. A strategy triggered by a registered symbol (e.g., `TXFD6`) can emit an order for an unregistered symbol (e.g., a new hedging instrument) and bypass the session gate entirely.

**Fix — both layers**:

#### 3.6.1 TrackGate: default to CLOSED

```python
def get_phase(self, symbol: str) -> SessionPhase:
    track = self._symbol_to_track.get(symbol)
    if track is None:
        if symbol not in self._warned_unknown:
            self._warned_unknown.add(symbol)
            logger.warning("track_gate_unknown_symbol_blocked",
                           symbol=symbol, default_phase="CLOSED")
        return SessionPhase.CLOSED
    return self._track_phases.get(track, SessionPhase.CLOSED)
```

#### 3.6.2 TrackGate: update `__slots__` and init

Current `__slots__` at `session_governor.py:75` is `("_symbol_to_track", "_track_phases")`. Must add `_warned_unknown`:

```python
__slots__ = ("_symbol_to_track", "_track_phases", "_warned_unknown")
```

Init:
```python
self._warned_unknown: set[str] = set()
```

#### 3.6.3 StrategyRunner: per-intent symbol gating

Replace the current per-event gate at `runner.py:684-693` with per-intent filtering:

```python
# Current (WRONG): gates entire batch on event.symbol
#   symbol = getattr(event, "symbol", "")
#   phase = self.track_gate.get_phase(symbol)
#   if phase == SessionPhase.CLOSE_ONLY:
#       intents = [i for i in intents if i.intent_type in (...)]
#   elif phase in (...):
#       intents = []

# Fixed: gate each intent on its own symbol
if getattr(self, "track_gate", None) is not None and intents:
    from hft_platform.ops.session_governor import SessionPhase

    _CLOSE_ONLY_TYPES = (IntentType.CANCEL, IntentType.FORCE_FLAT)
    filtered: list[OrderIntent] = []
    for intent in intents:
        phase = self.track_gate.get_phase(intent.symbol)
        if phase == SessionPhase.OPEN:
            filtered.append(intent)
        elif phase == SessionPhase.CLOSE_ONLY:
            if intent.intent_type in _CLOSE_ONLY_TYPES:
                filtered.append(intent)
            # else: dropped (only closing actions allowed)
        # else (FORCE_FLAT, CLOSED, PRE_OPEN, INIT): intent dropped
    intents = filtered
```

This ensures each intent is gated by **its own symbol's session phase**, not the triggering event's symbol.

#### 3.6.4 Override for research mode

Env var `HFT_TRACK_GATE_DEFAULT_OPEN=1` restores the old OPEN default behavior in `TrackGate.get_phase()`. Not recommended for production.

---

### D7: Intent Batch Partial Submit Protection

**Root cause**: `runner.py:760-786` — `_risk_submit(intent)` raises `QueueFull` mid-loop. Exception propagates unhandled, crashing the strategy runner task. Earlier intents in the batch are already submitted.

**Fix**:

#### 3.7.1 Per-intent try/except

```python
submitted = 0
dropped = 0
for intent in intents:
    try:
        self._risk_submit(intent)
        submitted += 1
    except asyncio.QueueFull:
        dropped += 1
        self.metrics.intent_queue_full_total.inc()
        logger.error("intent_submit_queue_full",
                     strategy_id=intent.strategy_id,
                     submitted=submitted, dropped=dropped,
                     batch_size=len(intents))
if dropped > 0:
    sid = intents[0].strategy_id if intents else "unknown"
    # Record circuit breaker failure inline (same pattern as runner.py:648-664)
    strategy = self.strategies.get(sid)
    if self._rust_circuit is not None:
        self._rust_circuit.record_failure(sid, time.monotonic_ns())
    else:
        failures = self._failure_counts.get(sid, 0) + 1
        self._failure_counts[sid] = failures
        half_threshold = max(1, self._circuit_threshold // 2)
        state = self._circuit_states.get(sid, "normal")
        if state == "normal" and failures >= half_threshold:
            self._circuit_states[sid] = "degraded"
            logger.warning("strategy_circuit_degraded",
                           id=sid, reason="queue_full_partial_batch")
        if failures >= self._circuit_threshold and state != "halted":
            self._circuit_states[sid] = "halted"
            if strategy is not None:
                strategy.enabled = False
            self._circuit_halted_at_ns[sid] = time.monotonic_ns()
            logger.error("strategy_circuit_halted",
                         id=sid, reason="queue_full_partial_batch")
```

**Note**: The circuit breaker failure logic is recorded inline, matching the existing pattern at `runner.py:648-664`. There is no `_record_circuit_failure` helper method in the current codebase — the logic is duplicated inline in multiple call sites. Extracting a helper is a separate refactoring concern, not part of this spec.

#### 3.7.2 Effects

- Strategy runner stays alive (no unhandled exception propagation).
- Circuit breaker is notified of the failure → strategy degrades/halts if persistent.
- Observability: `intent_queue_full_total` counter + per-event ERROR log with batch context.

---

### D8: StormGuard Halt Callback Error Handler

**Root cause**: `storm_guard.py:281` — `loop.create_task(result)` is fire-and-forget. Exceptions in the halt callback coroutine are swallowed by asyncio.

**Fix**:

```python
if asyncio.iscoroutine(result):
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(result)
        task.add_done_callback(self._halt_callback_done)
    except RuntimeError:
        logger.warning("halt_callback_coroutine_no_loop")
```

```python
def _halt_callback_done(self, task: asyncio.Task) -> None:
    exc = task.exception()
    if exc is not None:
        logger.error("halt_callback_failed",
                     error=str(exc),
                     error_type=type(exc).__name__)
```

---

## 4. Testing Strategy

Each defect gets dedicated unit tests:

| ID | Test Approach |
|----|---------------|
| D1 | Fill `raw_exec_queue` to capacity, fire `_safe_enqueue_exec` → assert overflow buffer populated, assert StormGuard HALT after 3. Test ExecutionRouter drains overflow before main queue. |
| D2 | Mock `place_order` with delay; fire `on_terminal_state` with broker `order_id` before `_register_broker_ids` → assert deferred to `_deferred_terminals`. After registration completes, assert `_drain_deferred_terminals` resolves and cleans `live_orders`. Test 30s expiry path. Assert `_pending_close_qty` acquires lock. |
| D3 | Mock `time.monotonic()` / `time.monotonic_ns()` to return decreasing values → assert cooldown still works correctly (never goes negative). Test each of 8 sites independently. |
| D4 | Fill `order_queue`, set `storm_guard.state = HALT`, call evaluate → assert command dropped with metric. Assert `put_nowait` used (no blocking). On `QueueFull`: assert DLQ entry created, assert StormGuard HALT triggered, assert DLQ bounded at 256. |
| D5 | Concurrent fill simulation (threading) during `snapshot_positions()` → assert snapshot is self-consistent (all positions from same "generation"). |
| D6 | **TrackGate**: `get_phase("UNKNOWN_SYMBOL")` → assert `CLOSED`. `HFT_TRACK_GATE_DEFAULT_OPEN=1` → assert `OPEN`. Warning logged once per symbol. **StrategyRunner**: Strategy triggered by registered symbol emits intent for unregistered symbol → assert intent filtered by per-intent gating. |
| D7 | Fill intent queue to capacity, submit batch of 5 intents → assert first N succeed, rest logged as dropped, circuit failure recorded, runner stays alive. |
| D8 | Register halt callback that raises → assert `halt_callback_failed` logged (not swallowed). |

---

## 5. New Metrics

The following metrics must be added to `MetricsRegistry.__init__` in `src/hft_platform/observability/metrics.py`:

| Attribute | Type | Prometheus Name | Used By |
|-----------|------|-----------------|---------|
| `exec_queue_overflow_total` | Counter | `exec_queue_overflow_total` | D1 |
| `exec_overflow_drained_total` | Counter | `exec_overflow_drained_total` | D1 |
| `exec_overflow_evicted_total` | Counter | `exec_overflow_evicted_total` | D1 |
| `terminal_before_registration_total` | Counter | `terminal_before_registration_total` | D2 |
| `deferred_terminal_expired_total` | Counter | `deferred_terminal_expired_total` | D2 |
| `risk_halt_blocked_total` | Counter | `risk_halt_blocked_total` | D4 |
| `order_queue_full_total` | Counter | `order_queue_full_total` | D4 |
| `intent_queue_full_total` | Counter | `intent_queue_full_total` | D7 |

All are unlabeled Counters — no cardinality risk. Must also be added to the `_unregister_metric_prefixes()` call at the top of `__init__` for safe test re-instantiation.

---

## 6. Files Modified

| File | Changes |
|------|---------|
| `src/hft_platform/services/system.py` | D1: `_safe_enqueue_exec`, `_exec_overflow_buf` init, pass to router |
| `src/hft_platform/execution/router.py` | D1: overflow drain in `run()` loop |
| `src/hft_platform/order/adapter.py` | D2: sentinel, `_pending_order_keys`, `_deferred_terminals`, `_drain_deferred_terminals`, `_pending_close_qty` lock |
| `src/hft_platform/risk/storm_guard.py` | D3: monotonic timestamps (3a-3c), D8: callback error handler |
| `src/hft_platform/order/circuit_breaker.py` | D3: monotonic timestamp (3d) |
| `src/hft_platform/strategy/runner.py` | D3: monotonic (3e-3f), D5: use `snapshot_positions()`, D6: per-intent symbol gating, D7: per-intent catch + inline circuit failure |
| `src/hft_platform/risk/engine.py` | D3: monotonic deadline (3g), D4: HALT guard + put_nowait + DLQ |
| `src/hft_platform/execution/positions.py` | D5: `snapshot_positions()` method |
| `src/hft_platform/ops/session_governor.py` | D6: default CLOSED, `__slots__` update, `_warned_unknown` |
| `src/hft_platform/observability/metrics.py` | D1/D2/D4/D7: 8 new Counter metrics |

---

## 7. Rollout

All 8 fixes are independent at the code level. Recommended implementation order:

0. **Metrics** (prerequisite) — add 8 new Counters to `MetricsRegistry`
1. **D8** (trivial, 3 lines) — warm-up, verify test infra
2. **D3** (8 surgical sites) — monotonic migration, each site independently testable
3. **D5** (atomic snapshot) — small, isolated
4. **D4** (HALT drain race + DLQ) — risk engine, small
5. **D7** (intent batch protection) — strategy runner hardening
6. **D6** (default CLOSED + per-intent gating) — two-layer fix, both TrackGate and StrategyRunner
7. **D1** (overflow ring buffer) — cross-component (system + router)
8. **D2** (live_orders TOCTOU) — most complex, deferred terminal queue

Each fix should be a separate commit for clean revert capability.

---

## 8. Out of Scope

- RL / stochastic control execution strategies (separate design round)
- `asyncio.get_event_loop()` deprecation migration (cosmetic, no correctness impact)
- RingBufferBus signal race (1-cycle delay, no data loss)
- Strategy quarantine → circuit failure acceleration (by-design behavior)
- Broker callback early-startup window (small window, guarded by `self.running` check)
