# Pipeline Chains — End-to-End Data Flow

> **Date**: 2026-04-06
> **Scope**: Runtime trading system data flow, traced from source code
> **Companion**: [current-architecture.md](current-architecture.md) (module inventory), [modules/](../modules/) (per-module docs)

This document traces each runtime pipeline chain end-to-end, identifying exact function call sites, data type transformations, queue boundaries, thread crossings, and backpressure behavior.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        HFT Platform Runtime Chains                      │
│                                                                         │
│  Chain 1: Market Data    Exchange → Broker → Normalize → LOB →          │
│                          Feature → RingBufferBus                        │
│                                                                         │
│  Chain 2: Decision       Bus → Strategy → Risk/Gateway → OrderAdapter   │
│                          → Broker API                                   │
│                                                                         │
│  Chain 3: Execution      Broker callbacks → ExecutionRouter →           │
│                          Positions → Reconciliation → Bus               │
│                                                                         │
│  Chain 4: Recording      Events → Batcher → ClickHouse / WAL           │
│                                                                         │
│  Chain 5: Safety         StormGuard FSM ↔ all planes                    │
│                          AutonomyMonitor → PlatformDegrade              │
│                                                                         │
│  Chain 6: Operations     SessionGovernor → TrackGate → Notifications    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Chain 1: Market Data Pipeline

**Path**: Exchange → BrokerFacade → raw_queue → Normalize → LOB → Feature → Bus

### Stage Diagram

```
Shioaji Thread              │  asyncio Event Loop Thread
                            │
[Exchange]                  │
  ↓                         │
BrokerCallback              │
  ↓                         │
_on_shioaji_event()  ───────┼──→ call_soon_threadsafe(_enqueue_raw)
  (broker thread)           │         ↓
                            │    raw_queue.put_nowait()
                            │         ↓ (async get)
                            │    MarketDataService.run()
                            │         ↓
                            │    _process_raw()
                            │      ├─ normalize_tick()  → TickEvent
                            │      └─ normalize_bidask() → BidAskEvent
                            │         ↓
                            │    LOBEngine.process_event() → LOBStatsEvent
                            │         ↓
                            │    FeatureEngine.process_lob_stats() → FeatureUpdateEvent
                            │         ↓
                            │    RingBufferBus.publish_nowait()
                            │      (lock-free, O(1))
```

### Hop-by-Hop Detail

| # | Function | File | Data In → Out | Mechanism | Latency |
|---|----------|------|---------------|-----------|---------|
| 1 | `_on_shioaji_event()` | `services/market_data.py:929` | Raw dict/obj → (exchange, msg) | Broker thread callback | **Blocking** (must be <1μs) |
| 2 | `call_soon_threadsafe()` | `services/market_data.py:992` | (exchange, msg) | **Thread boundary** | Async transition |
| 3 | `_enqueue_raw()` | `services/market_data.py:1425` | → `raw_queue` | `asyncio.Queue.put_nowait()` | Non-blocking O(1) |
| 4 | `run()` main loop | `services/market_data.py:641` | `raw_queue.get()` | Async dequeue | Event-loop wait |
| 5a | `normalize_tick()` | `feed_adapter/normalizer.py:596` | Raw → `TickEvent` (price x10000) | Inline, Rust optional | Sync <10μs |
| 5b | `normalize_bidask()` | `feed_adapter/normalizer.py:727` | Raw → `BidAskEvent` (np.int64) | Inline, Rust optional | Sync <10μs |
| 6 | `LOBEngine.process_event()` | `feed_adapter/lob_engine.py:594` | BidAsk/Tick → `LOBStatsEvent` | In-memory book | Sync <5μs |
| 7 | `FeatureEngine.process_lob_stats()` | `feature/engine.py:425` | LOBStats → `FeatureUpdateEvent` (27 features) | Inline, Rust kernel optional | Sync <20μs |
| 8 | `RingBufferBus.publish_nowait()` | `engine/event_bus.py:572` | Event → ring buffer cursor | Lock-free single-writer | Sync O(1) <1μs |

### Thread Boundaries

- **Only one crossing**: Broker thread → asyncio event loop via `call_soon_threadsafe()`
- Stages 4-8 all execute on **single asyncio event loop thread** — no locks needed
- Total pipeline latency (queue dequeue → bus publish): typically **10-100μs**

### Backpressure

- `raw_queue` full → **drop** message, increment `_raw_dropped_count`
- Never blocks broker thread (would stall all market data)

### Key Env Vars

| Variable | Default | Controls |
|----------|---------|---------|
| `HFT_FEATURE_ENGINE_ENABLED` | `1` | Skip stage 7 if `0` |
| `HFT_EVENT_MODE` | `event` | `tuple` mode skips dataclass allocation |
| `HFT_FUSED_NORMALIZER` | `0` | Fuse stages 5+6 into single Rust call |
| `HFT_RUST_ACCEL` | `1` | Enable Rust fast-paths in normalizer/LOB |

---

## Chain 2: Decision Pipeline

**Path**: Bus → Strategy → Risk → Order → Broker API

### Stage Diagram

```
RingBufferBus
  ↓ (async consume)
StrategyRunner.process_event()
  ↓
strategy.handle_event() → List[OrderIntent]
  ↓ (populate decision_price, filter by TrackGate)
  ↓
  ├─── [Without Gateway] ──────────────────────────┐
  │  risk_queue.put_nowait(intent)                  │
  │    ↓                                            │
  │  RiskEngine.run() → evaluate() → OrderCommand   │
  │    ↓                                            │
  │  order_queue.put_nowait(cmd)                    │
  │                                                 │
  ├─── [With Gateway] ─────────────────────────────┐│
  │  LocalIntentChannel.submit_nowait(intent)      ││
  │    ↓                                           ││
  │  GatewayService._process_envelope()            ││
  │    1. Dedup → 2. Policy → 3. Exposure          ││
  │    4. Risk → 5. Command → 6. Dispatch          ││
  │    7. Commit                                   ││
  │    ↓                                           ││
  │  order_queue.put_nowait(cmd)                   ││
  └────────────────────────────────────────────────┘│
                                                    │
  OrderAdapter.run()  ←─────────────────────────────┘
    ↓ (10-step pipeline: HALT check, dedup, rate limit,
    │   CB, degrade, shadow intercept, client validate)
    ↓
  _dispatch_to_api() → broker.place_order()
```

### Hop-by-Hop Detail

| # | Function | File | Data In → Out | Mechanism |
|---|----------|------|---------------|-----------|
| 1 | `StrategyRunner.run()` | `strategy/runner.py` | Bus consume → events | `async for event in bus.consume()` |
| 2 | `process_event()` | `strategy/runner.py` | Event → `List[OrderIntent]` | Inline dispatch to strategies |
| 3a | `risk_queue.put_nowait()` | `strategy/runner.py` | OrderIntent | `asyncio.Queue` (no gateway) |
| 3b | `channel.submit_nowait()` | `gateway/channel.py` | OrderIntent → IntentEnvelope | `LocalIntentChannel` (with gateway) |
| 4a | `RiskEngine.evaluate()` | `risk/engine.py` | OrderIntent → `RiskDecision` | Sync validator chain |
| 4b | `GatewayService._process_envelope()` | `gateway/service.py` | Envelope → 7-step pipeline | Dedup→Policy→Exposure→Risk→Cmd→Dispatch→Commit |
| 5 | `RiskEngine.create_command()` | `risk/engine.py` | OrderIntent → `OrderCommand` | Attach deadline_ns, storm_guard_state |
| 6 | `order_queue.put_nowait()` | — | OrderCommand | `asyncio.Queue` |
| 7 | `OrderAdapter.execute()` | `order/adapter.py` | OrderCommand → broker API call | 10-step validation pipeline |
| 8 | `_dispatch_to_api()` | `order/adapter.py` | → `broker.place_order()` | Thread pool via `asyncio.to_thread()` |

### Strategy Dispatch Details

- **Staleness guard**: Skip events older than `HFT_STALE_EVENT_THRESHOLD_MS` (500ms)
- **Circuit breaker**: 3-state FSM (Normal→Degraded→Halted) per strategy
- **Timeout**: Max `HFT_STRATEGY_TIMEOUT_MS` (50ms) per handler
- **Intent flood cap**: Max `HFT_MAX_INTENTS_PER_EVENT` (20) intents per event
- **Decision price**: Auto-populated from `LOBEngine.get_l1_scaled(symbol)`
- **Session filter**: TrackGate blocks NEW during CLOSE_ONLY/FORCE_FLAT phases

### OrderAdapter 10-Step Pipeline

```
1. HALT state check         → block NEW unless exempt
2. Idempotency dedup        → skip CANCEL/FORCE_FLAT
3. Per-symbol rate limit    → HARD → DLQ
4. Per-strategy circuit CB  → block if open
5. Global circuit breaker   → block if open
6. Global rate limit        → 180 soft / 250 hard per 10s
7. Platform degrade check   → reduce-only enforcement
8. Shadow mode intercept    → log, skip broker
9. Client validation        → hasattr checks
10. Dispatch or queue       → coalescing in _api_worker (5ms window)
```

### Backpressure

| Queue | On Full | Effect |
|-------|---------|--------|
| `risk_queue` | `QueueFull` → drop intent | `RiskFeedback` sent to strategy |
| `LocalIntentChannel` | `QueueFull` → reject | Envelope routed to DLQ |
| `order_queue` | `QueueFull` → DLQ entry | OrderCommand saved to dead letter |

### Key Env Vars

| Variable | Default | Controls |
|----------|---------|---------|
| `HFT_GATEWAY_ENABLED` | `0` | Route through 7-step gateway |
| `HFT_STRATEGY_TIMEOUT_MS` | `50` | Max handler execution time |
| `HFT_MAX_INTENTS_PER_EVENT` | `20` | Intent flood cap |
| `HFT_STALE_EVENT_THRESHOLD_MS` | `500` | Skip stale events |
| `HFT_ORDER_SHADOW_MODE` | `0` | Log orders without broker submission |
| `HFT_API_COALESCE_WINDOW_S` | `0.005` | 5ms order coalescing |
| `HFT_STRICT_PRICE_MODE` | `1` | Reject float prices |

---

## Chain 3: Execution Pipeline

**Path**: Broker callbacks → ExecutionRouter → Positions → Reconciliation

### Stage Diagram

```
Broker Thread                │  asyncio Event Loop Thread
                             │
[Broker order/deal callback] │
  ↓                          │
call_soon_threadsafe() ──────┼──→ raw_exec_queue.put_nowait()
                             │         ↓ (async get)
                             │    ExecutionRouter.run()
                             │         ↓
                             │    ExecutionNormalizer
                             │      ├─ .normalize_fill() → FillEvent
                             │      └─ .normalize_order() → OrderEvent
                             │         ↓
                             │    [FillEvent path]
                             │      ├─ Dedup check (OrderedDict, 10K bound)
                             │      ├─ PositionStore.on_fill() → PositionDelta
                             │      ├─ RiskEngine.notify_fill_pnl()
                             │      ├─ Bus.publish_nowait(PositionDelta)
                             │      ├─ Bus.publish_nowait(FillEvent)
                             │      └─ recorder_queue (direct + WAL fallback)
                             │
                             │    [OrderEvent path]
                             │      ├─ Bus.publish_nowait(OrderEvent)
                             │      └─ Terminal state → cleanup live_orders
```

### Thread Boundary Entry (`services/system.py:963-976`)

```python
def _on_exec(self, topic, data):
    # Runs in Shioaji broker thread
    event = RawExecEvent(topic, data, timebase.now_ns())
    self.loop.call_soon_threadsafe(self._safe_enqueue_exec, event)
```

- `_safe_enqueue_exec()` → `raw_exec_queue.put_nowait()`
- Queue full → overflow buffer (4K max) + metric
- 3+ consecutive overflows → `storm_guard.trigger_halt("exec_queue_overflow_repeated")`

### Key Mechanisms

- **Fill deduplication**: Bounded `OrderedDict` (10K entries), synthetic dedup key: `"{symbol}|{order_id}|{side}|{price}|{qty}|{match_ts_ns}"`
- **Orphaned fills**: strategy_id="UNKNOWN" → `OrphanedFillDLQ`, retry every 100 events
- **Position update**: Atomic under `_fill_lock` (threading.Lock) for Rust/Python dual-tracker
- **TCA enrichment**: `cmd_tca_map[order_id]` → `decision_price` + `arrival_price` stamped on FillEvent
- **Recording safety**: Direct `recorder_queue` write (bypasses Bus to prevent tick flood overwriting fills)
- **WAL fallback**: When `recorder_queue` full → WALWriter for fill durability

### Reconciliation Paths

| Type | Trigger | Action on Mismatch |
|------|---------|-------------------|
| **Periodic** | `ReconciliationService.sync_portfolio()` | Critical → HALT; non-critical → reduce-only after 2 observations |
| **Startup** | `StartupPositionVerifier.recover()` | Dual-source merge (checkpoint + broker); critical → HALT |
| **EOD** | `EODReconciliationRunner` at 05:00 UTC | Once-per-day settlement sync |
| **Checkpoint** | `PositionCheckpointWriter` every N seconds | Atomic JSON + SHA-256 integrity |

### Position PnL (Scaled Integer)

```
LONG close:  PnL = (fill_price - avg_price) × close_qty × multiplier
SHORT close: PnL = (avg_price - fill_price) × close_qty × multiplier
Avg price:   new_avg = (2×total_val + net_qty) // (2×net_qty)  [rounding correction]
```

---

## Chain 4: Recording Pipeline

**Path**: Events → Batcher → ClickHouse / WAL

### Two Recording Modes

```
Mode A: Direct (HFT_RECORDER_MODE=direct)
─────────────────────────────────────────
  recorder_queue.put_nowait()  ← MarketDataService, ExecutionRouter
       ↓ (async get)
  RecorderService.run()
       ↓
  Schema extractor (fast-path, avoids serialize())
       ↓
  Batcher.add() → ColumnarBuffer (double-buffer)
       ↓ (flush on count OR time threshold)
  Batcher.flush() → swap buffers (zero-copy)
       ↓
  DataWriter.write_columnar() → ClickHouse INSERT
       ↓ (on failure)
  WALWriter fallback → .wal/ directory


Mode B: WAL-First (HFT_RECORDER_MODE=wal_first)
────────────────────────────────────────────────
  recorder_queue.put_nowait()
       ↓
  RecorderService.run()
       ↓
  WALFirstWriter.write() → DiskPressureMonitor.get_level()
       ↓ (if OK/WARN)
  WALBatchWriter → .wal/ files (batch interval + max rows)
       ↓ (separate process)
  WALLoaderService.run() → poll .wal/ → parse JSONL
       ↓
  Shard claim (fcntl LOCK_EX) → ClickHouse INSERT
       ↓ (on failure)
  DLQ for failed rows
```

### Backpressure Cascade

```
recorder_queue full → DROP event (never block hot path)
  ↓
GlobalMemoryGuard budget exceeded → evict low-priority batchers
  ↓
DiskPressureMonitor levels:
  OK       → write normally
  WARN     → write, log warning
  CRITICAL → per-topic policy (write/drop/halt)
  HALT     → reject all writes
```

### GlobalMemoryGuard Priority

| Table | Priority | Evicted First? |
|-------|----------|---------------|
| `hft.fills` | 110 | Last |
| `hft.orders` | 105 | — |
| `hft.market_data` | 100 | — |
| `hft.risk_log` | 50 | — |
| `hft.latency_spans` | 10 | First |

### Key Env Vars

| Variable | Default | Controls |
|----------|---------|---------|
| `HFT_RECORDER_MODE` | `direct` | `direct` or `wal_first` |
| `HFT_RECORDER_DROP_ON_FULL` | `1` | Drop on queue full |
| `HFT_GLOBAL_BUFFER_MAX_ROWS` | `50000` | Cross-table memory budget |
| `HFT_WAL_BATCH_INTERVAL_MS` | `1000` | WAL batch flush interval |
| `HFT_WAL_BATCH_MAX_ROWS` | `5000` | WAL batch max rows |
| `HFT_WAL_HALT_MB` | `500` | WAL dir size → HALT |

---

## Chain 5: Safety Pipeline

**Path**: StormGuard FSM ← signals → HALT cascade → Autonomy degradation

### StormGuard FSM

```
                    ┌─────────────────────────────────────────┐
                    │            StormGuard FSM                │
                    │                                         │
  feed_gap ────────→│  NORMAL ──→ WARM ──→ STORM ──→ HALT    │
  drawdown_bps ───→│    ↑                              │     │
  latency_us ─────→│    └──── manual rearm ←───────────┘     │
  component fail ─→│         (cooldown + N consecutive clears)│
  bus overflow ───→│                                         │
  DriftBurst ─────→│  Escalation: instant (safety-first)     │
                    │  De-escalation: cooldown + hysteresis   │
                    └─────────────────────────────────────────┘
```

### Threshold Evaluation (Priority Order)

```python
# HALT triggers (highest priority)
drawdown_bps <= -200 (-2.0%)                          → HALT

# STORM triggers
drawdown_bps <= -100 (-1.0%)                          → STORM
latency_us >= 20_000 (20ms)                           → STORM
feed_gap_s >= 1.0 (when session_active)               → STORM
feature_failure_active OR norm_failure_active          → STORM

# WARM triggers
drawdown_bps <= -50 (-0.5%)                           → WARM
latency_us >= 5_000 (5ms)                             → WARM
```

### De-escalation Rules

- HALT → lower: requires `HFT_STORMGUARD_HALT_COOLDOWN_S` (60s) + 5 consecutive clears
- STORM → lower: requires `HFT_STORMGUARD_STORM_COOLDOWN_S` (30s) + 5 consecutive clears
- Component failures hold STORM with anti-flap hold (`_FEATURE_RECOVERY_HOLD_S` = 5s)

### Who Calls StormGuard?

| Caller | File:Line | Method | Trigger |
|--------|-----------|--------|---------|
| `HFTSystem._supervisor_tick()` | `services/system.py:530` | `update(drawdown_bps, latency_us, feed_gap_s)` | Periodic ~0.1s supervision loop |
| `HFTSystem._supervisor_tick()` | `services/system.py:546` | `update_with_lob(mid, spread, imbalance, ts)` | DriftBurstDetector toxicity |
| `MarketDataService` | `services/market_data.py` | `report_feature_failure()` / `report_norm_failure()` | FeatureEngine or Normalizer errors |
| `RingBufferBus` | `engine/event_bus.py` | `trigger_halt("bus_overflow_cascade")` | Sustained bus overflow |
| `ReconciliationService` | `execution/reconciliation.py` | `trigger_halt("reconciliation_critical")` | Critical position mismatch |
| `DailyLossLimitValidator` | `risk/validators.py` | `trigger_halt("daily_loss_hard")` | Hard loss limit breached |
| `HFTSystem._safe_enqueue_exec()` | `services/system.py:953` | `trigger_halt("exec_queue_overflow_repeated")` | 3+ consecutive exec queue overflows |

### HALT Effects

| Component | Check | Behavior |
|-----------|-------|----------|
| `RiskEngine.evaluate()` | `storm_guard.validate(intent)` | Block NEW; allow CANCEL/FORCE_FLAT |
| `GatewayPolicy.gate()` | `storm_guard.state >= HALT` | Block NEW/AMEND; allow CANCEL + halt-exempt |
| `OrderAdapter.execute()` | `cmd.storm_guard_state == HALT` | Block unless safety-exempt |
| `HaltCanceller` | On HALT callback | Auto-cancel all live orders (batches of 20, 5ms delay) |
| `HaltFlattener` | On HALT callback (if enabled) | Auto-flatten all positions with FORCE_FLAT |

### Autonomy Degradation Cascade

```
AutonomyMonitor._evaluate()
  ├─ Broker disconnect > 300s    → enter_reduce_only("broker_unavailable")
  ├─ Feed gap > 120s             → enter_reduce_only("feed_gap_majority")
  ├─ Reconnect flapping > 5      → enter_reduce_only("feed_reconnect_flapping")
  ├─ Queue depth > 5000          → enter_reduce_only("queue_depth_exceeded")
  ├─ RSS memory > 2048 MB        → enter_reduce_only("rss_unhealthy")
  ├─ WAL backlog > 200 files     → enter_reduce_only("wal_backlog_unhealthy")
  └─ Reconciliation drift 2+     → enter_reduce_only("reconciliation_drift")

PlatformDegradeController
  ├─ reduce_only_active = True
  ├─ allow_intent(NEW, opens_risk=True) → False
  ├─ allow_intent(CANCEL) → True
  └─ Auto-recovery: feed_reconnect/feed_gap reasons recover after 60s cooldown
     Non-recoverable reasons require manual rearm

ManualRearmGate
  └─ File-based: outputs/production_rollout/autonomy/runtime_state.json
```

### Evidence Trail

All transitions logged to:
- `{base_dir}/{YYYYMMDD}/state_timeline.jsonl` — machine-readable
- `{base_dir}/{YYYYMMDD}/alert_digest.md` — human-readable
- `{base_dir}/runtime_state.json` — current state

---

## Chain 6: Operations Pipeline

**Path**: Session lifecycle → Phase filtering → Notifications

### Session Phase State Machine

```
INIT → PRE_OPEN → OPEN → CLOSE_ONLY → FORCE_FLAT → CLOSED
                                                       ↓
                                                 DailyReportService
                                                       ↓
                                                 NotificationDispatcher
```

### Multi-Track Schedules

| Track | Product | Day Session | Night Session |
|-------|---------|-------------|---------------|
| `stock` | TWSE equities | 09:00-13:30 | — |
| `futures_day` | TAIFEX futures | 08:45-13:45 | — |
| `futures_night` | TAIFEX futures | — | 15:00-05:00 (next day) |

### TrackGate Intent Filtering

```python
# In StrategyRunner.process_event():
phase = track_gate.get_phase(intent.symbol)

OPEN:       all intents allowed
CLOSE_ONLY: only CANCEL, FORCE_FLAT, IOC
FORCE_FLAT: only CANCEL, FORCE_FLAT
CLOSED:     all intents blocked
```

### Phase Callbacks

| Phase Transition | Action |
|-----------------|--------|
| → PRE_OPEN | Health preflight checks |
| → OPEN | Enable trading, start strategies |
| → CLOSE_ONLY | Block new positions, allow closing |
| → FORCE_FLAT | `PositionFlattener.flatten_all()` |
| → CLOSED | `DailyReportService.on_session_closed()` → Telegram report |

### Notification Delivery

| Type | Channel | Retry | Rate Limit |
|------|---------|-------|------------|
| **Critical** (HALT, daily loss, margin) | Telegram + Webhook | 3x exponential backoff | No |
| **Non-critical** (reconnect, heartbeat) | Telegram only | None | 1/second |
| **AlertManager** | HTTP webhook → Telegram bridge | N/A | N/A |

### Boot Sequence (SystemBootstrapper)

```
1.  Config loading (5-layer merge)
2.  Secret validation (validate_secrets)
3.  Broker client initialization
4.  Bounded queue creation (5 queues)
5.  Schema migration (auto-apply ClickHouse DDL)
6.  SessionGovernor + AutonomyMonitor
7.  RecorderService (MUST be first — fill durability)
8.  MarketDataService
9.  ExecutionRouter + ExecutionGateway
10. RiskEngine or GatewayService
11. OrderAdapter
12. Position recovery (StartupPositionVerifier)
13. CheckpointWriter
14. ReconciliationService
15. StrategyRunner
16. HealthServer (/healthz, /readyz, /status)
```

---

## Cross-Chain Interactions

### Queue Topology

```
                    ┌──────────────┐
                    │  raw_queue   │ ← Broker MD callbacks
                    │  (bounded)   │
                    └──────┬───────┘
                           ↓
                  MarketDataService
                           ↓
              ┌────────────────────────┐
              │    RingBufferBus       │ ← publish_nowait()
              │    (65536 ring)        │
              └───┬──────────┬─────┬──┘
                  ↓          ↓     ↓
          StrategyRunner  Recorder  (other consumers)
                  ↓
         ┌────────────────┐     ┌──────────────────┐
         │  risk_queue    │ OR  │ LocalIntentChannel│
         │  (bounded)     │     │ (TTL + DLQ)       │
         └───────┬────────┘     └────────┬──────────┘
                 ↓                       ↓
           RiskEngine            GatewayService
                 ↓                       ↓
         ┌───────────────┐
         │  order_queue  │
         │  (bounded)    │
         └───────┬───────┘
                 ↓
           OrderAdapter → Broker API

                    ┌──────────────────┐
                    │  raw_exec_queue  │ ← Broker exec callbacks
                    │  (bounded)       │
                    └──────┬───────────┘
                           ↓
                    ExecutionRouter
                     ├─→ PositionStore
                     ├─→ RingBufferBus (republish)
                     └─→ recorder_queue

         ┌──────────────────┐
         │  recorder_queue  │ ← MD events + fills
         │  (bounded, drop) │
         └──────┬───────────┘
                ↓
          RecorderService → ClickHouse / WAL
```

### Invariants

1. **Recording NEVER blocks hot path** — `put_nowait()` with drop policy
2. **Normalizer output always scaled integers** (x10000) — never raw floats downstream
3. **Event timestamps use `timebase.now_ns()`** (monotonic-aligned) — never `datetime.now()`
4. **Broker callbacks run in separate thread** — must use `call_soon_threadsafe()` to cross into event loop
5. **HALT blocks new orders** — CANCEL and FORCE_FLAT always allowed
6. **Fill recording uses direct queue** (not Bus) — prevents tick flood from overwriting fills

<!-- AUTO-GENERATED from source code trace 2026-04-06 -->
