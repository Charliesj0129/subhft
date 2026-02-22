# Design Review Artifacts

Date: 2026-02-21
Skill: `hft-architect` (5-step design review)
Scope: CE-M2 (GatewayService), CE-M3 (WALFirstWriter, DiskPressureMonitor), Alpha Governance

Each artifact follows the 5-gate hft-architect review framework:
1. **Allocation Audit** — heap allocations on hot path?
2. **Latency Budget** — microseconds added?
3. **Threading Model** — does this block the event loop?
4. **Data Layout** — is data cache-friendly?
5. **Failure Mode** — what happens on crash/pressure?

---

## CE-M2: GatewayService

**Code anchor**: `src/hft_platform/gateway/service.py`
**Status**: Implemented (2026-02-21)

### 5-Gate Review

| Gate | Question | Answer |
|------|----------|--------|
| Allocation Audit | Heap allocations on hot path? | No per-intent allocation. `ExposureKey` is a `slots=True` dataclass. Dedup uses a pre-sized `deque`. `IntentEnvelope` is created once per enqueue; no copies inside `_process_envelope`. |
| Latency Budget | Microseconds added per intent? | Dict lookup for dedup: ~100–300 ns. Exposure `check_and_update` with lock: ~200–500 ns. Risk `evaluate()` (CPU-only): ~1–5 µs depending on rule count. Total gateway overhead: ~1–10 µs in-process. |
| Threading Model | Blocks the event loop? | `GatewayService.run()` is a single `asyncio` task. `RiskEngine.evaluate()` is synchronous CPU-only (no I/O). `OrderAdapter._api_queue.put_nowait()` is non-blocking. Dedup `persist()` runs in `asyncio.to_thread()` on shutdown only. |
| Data Layout | Cache-friendly? | `ExposureStore._exposure` is a nested `dict[str, dict[str, dict[str, int]]]` of scaled integers. No pointer chasing on hot path for known (account, strategy, symbol) tuples. `IdempotencyStore` uses a fixed-capacity deque of small structs. |
| Failure Mode | Gateway crash behavior? | `FailSafePolicy FSM`: `NORMAL → DEGRADED → HALT`. Policy gate is called before every dispatch. Any uncaught exception in `_process_envelope` is logged and the envelope is dropped; the loop continues. Dedup state is persisted on clean shutdown. |

### Hardening Backlog (TODO)

- [ ] CE2-07: Add `gateway_dispatch_latency_ns` histogram, `gateway_reject_total`, `gateway_dedup_hits_total` to Prometheus dashboard
- [ ] CE2-08: Chaos test — kill gateway mid-flight, verify no duplicate broker dispatch
- [ ] CE2-09: Active/standby gateway with leader lease; only leader dispatches to broker
- [ ] CE2-10: Shioaji callbacks isolated to enqueue-only path with bounded queue
- [ ] CE2-11: `quote_version=v1` enforced with schema guard and reject-and-alert

---

## CE-M2: ExposureStore

**Code anchor**: `src/hft_platform/gateway/exposure.py`
**Status**: Implemented with memory bound (CE2-12, 2026-02-21)

### 5-Gate Review

| Gate | Question | Answer |
|------|----------|--------|
| Allocation Audit | Heap allocations on hot path? | No per-check allocation. Dict entries are created only on first-seen (account, strategy, symbol) tuple. `ExposureKey` is a `slots=True` dataclass. |
| Latency Budget | Microseconds added? | `threading.Lock()` acquisition + 3-level nested dict lookup + integer arithmetic: ~200–500 ns. |
| Threading Model | Blocks the event loop? | No. Lock scope is minimal (dict lookup + int arithmetic only). All hot-path ops are O(1) under lock. |
| Data Layout | Cache-friendly? | Nested `dict[str, dict[str, dict[str, int]]]`; all values are scaled integers (Precision Law compliant). |
| Failure Mode | Symbol dict grows unbounded? | Fixed by CE2-12: `_max_symbols` bound (default 10,000, env `HFT_EXPOSURE_MAX_SYMBOLS`). On overflow: zero-balance eviction first; if still full, `ExposureLimitError` raised and logged. |

### Memory Bound Details (CE2-12)

- `_symbol_count` tracks total leaf entries (incremented on new insert, decremented by `_evict_zeroes()`).
- `_evict_zeroes()` is called under lock only when `_symbol_count >= _max_symbols` and a new symbol is requested.
- Worst-case eviction is O(N) but only triggered at capacity; normal hot-path is O(1).
- `ExposureLimitError` is a `RuntimeError` subclass; callers (GatewayService) must handle it.

---

## CE-M2: IdempotencyStore (DeduplicationGuard)

**Code anchor**: `src/hft_platform/gateway/dedup.py`
**Status**: Implemented (2026-02-21)

### 5-Gate Review

| Gate | Question | Answer |
|------|----------|--------|
| Allocation Audit | Heap allocations on hot path? | Pre-allocated `deque(maxlen=N)` at startup. `DedupRecord` is a `slots=True` dataclass. No per-intent heap allocation beyond the initial `DedupRecord` insertion. |
| Latency Budget | Microseconds added? | Lock + deque lookup: ~100–300 ns. Dict lookup in window: O(1) amortized. |
| Threading Model | Blocks the event loop? | No. Lock scope is minimal. `persist()` (file I/O) is only called off-loop on shutdown. |
| Data Layout | Cache-friendly? | Fixed-capacity `deque` of `slots=True` dataclasses. Window indexed by `idempotency_key` string. |
| Failure Mode | Dedup state lost on crash? | `persist()` writes state to disk on clean shutdown. On restart, state is optionally loaded back. Cold-restart dedup window is empty — within-window TTL determines replay risk. |

---

## CE-M2: GatewayPolicy (FailSafePolicy FSM)

**Code anchor**: `src/hft_platform/gateway/policy.py`
**Status**: Implemented (2026-02-21)

### 5-Gate Review

| Gate | Question | Answer |
|------|----------|--------|
| Allocation Audit | Heap allocations on hot path? | No. Policy mode is an enum state. `gate()` returns a `(bool, str)` tuple — no object allocation. |
| Latency Budget | Microseconds added? | Enum comparison + StormGuard state read: ~50–100 ns. |
| Threading Model | Blocks the event loop? | No. Policy state transitions are atomic enum assignments. |
| Data Layout | Cache-friendly? | Single `IntEnum` state field + static string constants. |
| Failure Mode | Policy in HALT mode? | All new intents rejected with `POLICY_HALT` reason. CANCEL intents are permitted regardless of policy mode (safe cancel semantics). |

---

## CE-M3: WALFirstWriter

**Code anchor**: `src/hft_platform/recorder/wal_first.py`
**Status**: Implemented (2026-02-21)

### 5-Gate Review

| Gate | Question | Answer |
|------|----------|--------|
| Allocation Audit | Heap allocations on hot path? | Pre-opened `WALBatchWriter` file handles at startup. `write()` method receives pre-built `list[dict]` from batcher; no additional copies. |
| Latency Budget | Microseconds added? | `DiskPressureMonitor.get_level()`: ~50 ns (lock + enum read). WAL batch append (OS write): ~50–200 µs per batch (not per event). Runtime path never waits on ClickHouse network. |
| Threading Model | Blocks the event loop? | `write()` is `async`; awaits `WALBatchWriter.add()` which may flush in a background thread. Does not block the main asyncio loop. |
| Data Layout | Cache-friendly? | WAL files are sequential byte streams (append-only). `DiskPressureLevel` is an `IntEnum` scalar. |
| Failure Mode | Disk pressure response? | `HALT` level → returns `False` immediately (caller logs data loss event). `CRITICAL` level → applies per-topic policy: `halt` (drop), `drop` (drop with warning), `write` (continue). |

### Hardening Backlog (TODO)

- [ ] CE3-03: Scale-out WAL loader workers with shard-claim protocol
- [ ] CE3-04: Full replay safety contract (ordering + dedup + manifest invariants) tested
- [ ] CE3-06: WAL SLO metrics: backlog size, replay lag, replay throughput, drain ETA
- [ ] CE3-07: Outage drills: ClickHouse down, slow, WAL disk-full, loader restart

---

## CE-M3: DiskPressureMonitor

**Code anchor**: `src/hft_platform/recorder/disk_monitor.py`
**Status**: Implemented (2026-02-21)

### 5-Gate Review

| Gate | Question | Answer |
|------|----------|--------|
| Allocation Audit | Heap allocations on hot path? | None on the hot path. `get_level()` and `get_topic_policy()` are read-only operations. Background `_check()` iterates `os.listdir()` but runs in daemon thread, not hot path. |
| Latency Budget | Microseconds added? | `get_level()`: ~50 ns (lock + IntEnum read). `get_topic_policy()`: `os.getenv()` call — not on hot path (called per-write, not per-event). |
| Threading Model | Blocks the event loop? | No. Runs as a daemon background thread (`disk-monitor`). `threading.Lock()` protects level and hooks. No asyncio interaction. |
| Data Layout | Cache-friendly? | `DiskPressureLevel` is a single `IntEnum` scalar. Hook list is small (typically 1–3 callbacks). |
| Failure Mode | Monitor thread crash? | `_loop()` catches all exceptions and logs a warning, then sleeps and retries. Monitor failure does not propagate to WAL writer path; level remains at last known value. |

---

## Alpha Governance Pipeline

**Code anchor**: `src/hft_platform/alpha/` (validation, promotion, canary, pool, experiments, audit)
**Status**: All 6 stages implemented (2026-02-21)

### 5-Gate Review

| Gate | Question | Answer |
|------|----------|--------|
| Allocation Audit | Heap allocations on hot path? | N/A — alpha pipeline is offline-only (CLI-invoked). No hot path involvement. |
| Latency Budget | Microseconds added? | N/A — offline pipeline. Backtest runs are research-time operations measured in seconds, not microseconds. |
| Threading Model | Blocks the event loop? | N/A — synchronous, CLI-invoked. No asyncio interaction. |
| Data Layout | Cache-friendly? | Offline: `dict[str, float]` for scorecards, YAML for promotion configs, JSON for experiment metadata. `float` is acceptable for research metrics (Rule 11 exception). |
| Failure Mode | Gate failure behavior? | Fail-fast with explicit `ValidationError`, `PromotionError`, etc. No silent data loss — all gate decisions written to audit log (`audit.alpha_*` tables). |

### Alpha Governance Architecture Notes

- **float exception (Rule 11)**: `alpha/` and `research/` modules use `float` for scorecard metrics (Sharpe, drawdown, etc.). Precision Law applies only to live trading accounting paths.
- **Audit trail**: Every gate decision (approve/reject) is written to ClickHouse `audit.alpha_*` via `src/hft_platform/alpha/audit.py`.
- **Audit bootstrap gap (D5)**: Audit schema (`src/hft_platform/schemas/audit.sql`) is not auto-applied by current `apply_schema()` path. Manual bootstrap required until M1 is complete.
- **Canary integration**: Canary lifecycle reads from `config/strategy_promotions/YYYYMMDD/<alpha_id>.yaml` and writes back hold/escalate/rollback/graduate decisions + audit rows.

### Pipeline Stage Map

| Stage | Module | Gate | CLI Command |
|-------|--------|------|-------------|
| Scaffold | `research/tools/alpha_scaffold.py` | — | `hft alpha scaffold` |
| Validation | `src/hft_platform/alpha/validation.py` | A/B/C | `hft alpha validate` |
| Pool analysis | `src/hft_platform/alpha/pool.py` | — | `hft alpha pool` |
| Combinatorial search | `research/combinatorial/search_engine.py` | — | `hft alpha search` |
| Promotion | `src/hft_platform/alpha/promotion.py` | D/E | `hft alpha promote` |
| Canary | `src/hft_platform/alpha/canary.py` | — | `hft alpha canary` |
| Audit | `src/hft_platform/alpha/audit.py` | — | (auto-called by all gates) |
| Experiments | `src/hft_platform/alpha/experiments.py` | — | `hft alpha experiments` |
| RL integration | `research/rl/lifecycle.py` | D/E via promote | `hft alpha rl-promote` |
