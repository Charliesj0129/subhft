# Slice C — Replay-Diff Parity Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the alpha promotion path (Gate C, optionally surfaced at Gate D) refuse alphas whose live `OrderIntent` stream diverges from an offline replay of the same market input. Default to a synthetic R47-OE1 divergence fixture; expose path-(a) live-reconstruction as a user-gated follow-up.

**Architecture:** Profile-driven blocking (inherited from Slice A). The new `ReplayParityGate` is a pure sub-gate evaluated against a `BacktestResult` whose new `replay_parity` payload carries `match_pct`, `n_compared`, `first_divergence_idx`, `divergence_histogram`, `evidence_path`, `harness_version`. The gate is registered into `vm_ul6_strict.yaml :: blocking_sub_gates` so loose `make research` runs are unaffected.

The runtime side adds an opt-in `intents` recorder topic that batches `OrderIntent` rows into a new ClickHouse table `hft.order_intents` (with WAL fallback). Recording is **disabled by default** behind `HFT_INTENT_RECORDER_ENABLED=0` and uses the same `put_nowait`-with-drop semantics that govern the existing market_data path so the hot path remains non-blocking.

A new `replay/strategy_replay.py` harness drives `RingBufferBus → StrategyRunner.process_event` with a deterministic clock and seeded RNG, replaying market data from a WAL fixture and capturing the strategy's emitted `OrderIntent` stream into a `ReplayedIntentLog`.

`alpha/replay_parity.py` produces an `IntentDiff` between live and replayed intent streams; `_sub_gates/replay_parity.py` consumes the diff and returns a `SubGateResult`. `promotion.py :: _evaluate_gate_d` adds a mirroring `replay_parity_audit` check (advisory at Gate D, blocking under strict profile via Slice A's existing `_invoke_sub_gates()` aggregation).

**Tech Stack:** Python 3.12, `pytest`, `numpy`, `PyYAML`, `msgspec`/`json` for canonical intent hashing, existing recorder `Batcher` pattern, existing `_sub_gates` registry from Slice A, existing `_validation_profile` loader from Slice A, existing `RingBufferBus` + `StrategyRunner.process_event` event loop.

---

## Spec reference

Source spec (plan-mode): `/home/charlie/.claude/plans/curried-launching-unicorn.md` (master blueprint, approved 2026-05-04 with Codex VERDICT: ACCEPT-WITH-FIXES applied).

Six locked decisions from the blueprint:

1. **Inherit Slice A patterns end-to-end.** Profile-driven blocking via `vm_ul6_strict.yaml :: blocking_sub_gates`, subagent-driven execution, pre-flight WIP-park, `make ci` green = done.
2. **DoD-C1 default = path (b).** Synthetic divergence fixture injects an R47-OE1-shaped cancel-path short-circuit. Path (a) — live reconstruction from `hft.orders` `OrderCommand` rows — is a Task 14 user-authorization gate, not a default.
3. **DoD-C2.** A clean reference strategy (deterministic 1-tick echo) must produce 100% intent parity under the same harness — the gate is not always-fail.
4. **DoD-C3.** `make ci` green; new tests under `tests/integration/test_replay_parity_e2e.py` cover both DoD-C1 and DoD-C2.
5. **DoD-C4.** Cold-start subagent can pick up any task in this plan and execute without prior context (Slice A pattern).
6. **No live OrderIntent recorder existed at blueprint time.** The new `intents` topic is opt-in, default-off, and never blocks the hot path.

Existing convention to follow (from Slice A):

- Each gate is a class with `name: str` (snake_case), `applies_to: set[str]`, `evaluate(result, config, thresholds) → SubGateResult`.
- Gates auto-register via `ensure_builtin_sub_gates_registered()` in `_sub_gates/__init__.py`.
- `result` is a `BacktestResult` (built in `_invoke_sub_gates_advisory`) — the new `replay_parity` field is added as an optional payload set by the offline replay harness.
- `SubGateResult.metrics` carries `match_pct`, `n_compared`, `first_divergence_idx`.

Codex CRITICAL pre-condition (from blueprint review 2026-05-04):

> `src/hft_platform/recorder/worker.py:302-315` registers topics for market_data / orders / fills / pnl_snapshots only — **no intent topic**. Slice C cannot assume any live intent stream existed for 2026-04-21. Reproducing R47 parity divergence requires either (a) reconstructing intents from the recorded `OrderCommand` stream that `hft.orders` already contains, or (b) building a synthetic divergence fixture. **Plan defaults to (b)** and tasks Task 14 with the optional (a) work.

---

## Pre-flight

**Working tree state on 2026-05-04 21:58:** clean on `main` at commit `41bdfee4` ("feat(ops): heartbeat tmpfs + core-dump capture forensics (#338)"). PR #337 (Slice A) merged; PR #338 (heartbeat ops) merged. No WIP-park required.

**Fixture preservation (already done — 2026-05-04 21:58):**

- 6,307 WAL shards for 2026-04-21 (`hft.market_data` only — no order intents) archived to:
  - Path: `~/.local/share/hft-fixtures/wal_2026_04_21.tar.gz`
  - Size: 37 MB compressed
  - SHA256: `c0ab51807cfb62dde56ec580d4e146515367f6270269d2c3a481b4d110bf140e`
  - Retention: outside git (too large to commit). Tasks reference the SHA; if missing, regenerate from `.wal/archive/` while the source still exists. The blueprint risk register flagged `HFT_WAL_RETENTION_DAYS=7` cleanup; archive captured 24 days post-event because cleanup had not yet run.

**Live `hft.orders` access (path (a)) is permission-gated.** A read attempt against the running ClickHouse for `2026-04-21 OrderCommand` rows was denied during planning. Path (a) is therefore deferred to Task 14, which begins by asking the user to authorize a one-time export query.

**Pre-flight task before starting Task 1:**

```bash
cd /home/charlie/hft_platform
git status --short --untracked-files=all
# Working tree must be clean. If not:
#   (a) commit / stash unrelated changes on a different branch; or
#   (b) verify with the user that those changes are out of scope for Slice C.

git switch -c slice-c/replay-parity-gate   # work branch off clean main
make ci                                    # must be green before Task 1

# Verify fixture exists; regenerate if missing.
test -f ~/.local/share/hft-fixtures/wal_2026_04_21.tar.gz \
  && sha256sum -c <(echo "c0ab51807cfb62dde56ec580d4e146515367f6270269d2c3a481b4d110bf140e  $HOME/.local/share/hft-fixtures/wal_2026_04_21.tar.gz")
```

If `make ci` is not green for reasons unrelated to Slice C, **stop and surface to the user** rather than starting Task 1.

---

## File structure

### New files (all under `/home/charlie/hft_platform`)

| Path | Responsibility |
|---|---|
| `src/hft_platform/replay/__init__.py` | Empty package marker |
| `src/hft_platform/replay/wal_fixture_loader.py` | Load 2026-04-21 tar.gz fixture → iterator of `MarketDataEvent` |
| `src/hft_platform/replay/strategy_replay.py` | Drive `RingBufferBus → StrategyRunner.process_event` deterministically; capture intents |
| `src/hft_platform/replay/intent_log.py` | `ReplayedIntentLog` collector with canonical-form serializer |
| `src/hft_platform/alpha/replay_parity.py` | `IntentDiff(live, replayed) → ReplayParityReport(match_pct, n_compared, first_divergence_idx, divergence_histogram)` |
| `src/hft_platform/alpha/_sub_gates/replay_parity.py` | `ReplayParityGate(applies_to={'maker','taker'})` |
| `src/hft_platform/migrations/clickhouse/20260504_001_create_order_intents.sql` | New `hft.order_intents` table (filename follows existing `YYYYMMDD_NNN_*.sql` convention — see `20260427_001_audit_schema_alignment.sql`) |
| `tests/fixtures/replay_parity/synthetic_r47_oe1_divergence.json` | Path-(b) synthetic divergence intents (handwritten) |
| `tests/fixtures/replay_parity/clean_echo_intents.json` | Reference deterministic 1-tick echo intents |
| `tests/unit/replay/test_wal_fixture_loader.py` | Loader tests |
| `tests/unit/replay/test_strategy_replay.py` | Harness determinism tests |
| `tests/unit/replay/test_intent_log.py` | Canonical-form hashing tests |
| `tests/unit/alpha/test_replay_parity.py` | `IntentDiff` unit tests |
| `tests/unit/alpha/test_sub_gate_replay_parity.py` | `ReplayParityGate` unit tests |
| `tests/integration/test_replay_parity_e2e.py` | DoD-C1 (synthetic R47 FAIL) + DoD-C2 (clean PASS) end-to-end |

### Modified files

| Path | Change |
|---|---|
| `src/hft_platform/recorder/worker.py` | Add `intents` topic registration (~line 302) and `Batcher` (~line 408); opt-in via `HFT_INTENT_RECORDER_ENABLED` |
| `src/hft_platform/recorder/writer.py` | Add intent insert path (mirror existing topic write helpers) |
| `src/hft_platform/recorder/__init__.py` | Export `INTENT_COLUMNS` if introduced |
| `src/hft_platform/alpha/_sub_gates/__init__.py` | Register `ReplayParityGate` in `ensure_builtin_sub_gates_registered()` |
| `config/research/profiles/vm_ul6_strict.yaml` | Add `replay_parity` to `blocking_sub_gates`; thresholds `replay_parity_match_pct_min: 95.0` |
| `src/hft_platform/alpha/promotion.py` | Add `replay_parity_audit` to `_evaluate_gate_d` (around line 320, mirror `latency_profile`) |
| `src/hft_platform/alpha/_validation_types.py` | Add optional `replay_parity_report: Any \| None = None` to `BacktestResult` |
| `tests/unit/test_alpha_promotion.py` | Add `test_promotion_uses_replay_parity_audit` |

---

## Task 1: WAL fixture loader — `replay/wal_fixture_loader.py`

Pure utility: given the tar.gz fixture, yield `MarketDataEvent` rows in `exch_ts` order. Foundation for the replay harness.

**Files:**
- Create: `src/hft_platform/replay/__init__.py` (empty)
- Create: `src/hft_platform/replay/wal_fixture_loader.py`
- Test: `tests/unit/replay/test_wal_fixture_loader.py`

- [ ] **Step 1.1: Write the failing tests**

The test suite covers four behaviors:
- `test_load_market_data_events_orders_by_exch_ts`: pass a fixture with two shards whose `exch_ts` are 200 and 100; expect output sorted ascending.
- `test_load_market_data_events_skips_non_market_data`: shard whose header has `__wal_table__: "hft.fills"` yields nothing.
- `test_load_market_data_events_rejects_missing_fixture`: nonexistent path raises `FixtureLoadError`.
- `test_load_market_data_events_filters_by_symbol`: passing `symbols={"TMFD6"}` filters out other symbols.

Each test builds a fake `.tar.gz` via `tarfile` containing JSONL shards with `__wal_table__` headers (mirror real archive layout).

- [ ] **Step 1.2: Run the tests — confirm RED for the right reason**

```bash
uv run pytest tests/unit/replay/test_wal_fixture_loader.py -q --tb=short
# Expected: ImportError on hft_platform.replay.wal_fixture_loader (module does not exist)
```

The fail must be "module not found" — not a fixture syntax error.

- [ ] **Step 1.3: Implement `wal_fixture_loader.py`**

```python
# src/hft_platform/replay/wal_fixture_loader.py
"""Load .wal/archive tar.gz fixtures into ordered MarketDataEvent dicts.

Public API:
    load_market_data_events(path, symbols=None) -> Iterator[dict]

Each yielded dict is a single market_data row (BidAsk or Tick) from the
WAL archive, sorted by exch_ts ascending across all shards.
"""
from __future__ import annotations

import json
import tarfile
from collections.abc import Iterator
from pathlib import Path


class FixtureLoadError(RuntimeError):
    pass


def _iter_shard_rows(reader) -> Iterator[dict]:
    """Yield body rows from a single jsonl shard. Skips header (first line)."""
    first = True
    for raw in reader:
        line = raw.strip()
        if not line:
            continue
        if first:
            first = False
            try:
                header = json.loads(line)
            except Exception:
                return
            if header.get("__wal_table__") != "hft.market_data":
                return
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def load_market_data_events(
    path: str | Path,
    *,
    symbols: set[str] | None = None,
) -> Iterator[dict]:
    """Yield market_data rows from a .tar.gz WAL fixture, sorted by exch_ts."""
    p = Path(path)
    if not p.exists():
        raise FixtureLoadError(f"fixture not found: {p}")
    rows: list[dict] = []
    with tarfile.open(p, "r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            if not member.name.endswith(".jsonl"):
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            for row in _iter_shard_rows(line.decode("utf-8") for line in f):
                if symbols is not None and row.get("symbol") not in symbols:
                    continue
                rows.append(row)
    rows.sort(key=lambda r: int(r.get("exch_ts", 0)))
    yield from rows
```

- [ ] **Step 1.4: Re-run tests — confirm GREEN**

```bash
uv run pytest tests/unit/replay/test_wal_fixture_loader.py -q --tb=short
# Expected: 4 passed
```

- [ ] **Step 1.5: Commit**

```bash
git add src/hft_platform/replay/__init__.py src/hft_platform/replay/wal_fixture_loader.py tests/unit/replay/test_wal_fixture_loader.py
git commit -m "feat(replay): WAL fixture loader for parity harness (Slice C task 1)"
```

**Verification:** loader yields rows in `exch_ts` order, skips non-market_data shards, filters by symbol, raises `FixtureLoadError` on missing fixture.

---

## Task 2: ClickHouse migration — `hft.order_intents` table

The new opt-in `intents` recorder topic needs a target table. Create the migration first so subsequent tasks can reference its schema.

**Files:**
- Create: `src/hft_platform/migrations/clickhouse/20260504_001_create_order_intents.sql`

- [ ] **Step 2.1: Confirm filename + suffix**

```bash
ls src/hft_platform/migrations/clickhouse/ | tail -5
# Existing convention: YYYYMMDD_NNN_<verb>_<noun>.sql (e.g. 20260427_001_audit_schema_alignment.sql).
# Use today's date for YYYYMMDD; if a 20260504_NNN_*.sql already exists, bump NNN.
```

- [ ] **Step 2.2: Author the SQL**

```sql
-- src/hft_platform/migrations/clickhouse/20260504_001_create_order_intents.sql
-- Slice C: opt-in OrderIntent recorder topic.
-- Activated by HFT_INTENT_RECORDER_ENABLED=1; otherwise table stays empty.
CREATE TABLE IF NOT EXISTS hft.order_intents (
    intent_id          Int64,
    strategy_id        LowCardinality(String),
    symbol             LowCardinality(String),
    intent_type        LowCardinality(String),  -- NEW / AMEND / CANCEL / FORCE_FLAT
    side               LowCardinality(String),  -- BUY / SELL
    price_scaled       Int64 CODEC(DoubleDelta, LZ4),
    qty                Int64 CODEC(DoubleDelta, LZ4),
    tif                LowCardinality(String),  -- LIMIT / IOC / FOK / ROD
    target_order_id    String,
    timestamp_ns       Int64 CODEC(DoubleDelta, LZ4),
    source_ts_ns       Int64 CODEC(DoubleDelta, LZ4),
    decision_price     Int64 CODEC(DoubleDelta, LZ4),
    price_type         LowCardinality(String),
    trace_id           String,
    idempotency_key    String,
    ttl_ns             Int64 CODEC(DoubleDelta, LZ4),
    reason             String CODEC(ZSTD(3)),
    ingest_ts          Int64 CODEC(DoubleDelta, LZ4),
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(toDateTime64(ingest_ts/1e9, 3))
ORDER BY (strategy_id, symbol, timestamp_ns, intent_id)
TTL toDate(toDateTime64(ingest_ts/1e9, 3)) + INTERVAL 365 DAY  -- aligned with hft.orders 365d retention (see 20260302_001_add_ttl_policies.sql)
SETTINGS index_granularity = 8192;
```

**Schema rationale:** mirrors `OrderIntent` at `src/hft_platform/contracts/strategy.py:36`. Enums stored as `LowCardinality(String)` for grep-ability; `intent_id`/`qty`/`price_scaled` keep their `Int64` shape so they round-trip through the `Batcher` extractor unchanged. `contract` (optional `ContractRef`) is intentionally omitted — symbol+price+qty are sufficient for parity diff; richer reconstruction is a follow-up.

- [ ] **Step 2.3: Verify migration loads (offline only)**

```bash
uv run python -c "
from pathlib import Path
sql = Path('src/hft_platform/migrations/clickhouse/20260504_001_create_order_intents.sql').read_text()
assert 'CREATE TABLE IF NOT EXISTS hft.order_intents' in sql
assert 'intent_id' in sql
print('migration parses')
"
```

- [ ] **Step 2.4: Commit**

```bash
git add src/hft_platform/migrations/clickhouse/20260504_001_create_order_intents.sql
git commit -m "feat(recorder): hft.order_intents migration (Slice C task 2)"
```

**Verification:** sequential filename, `IF NOT EXISTS` (idempotent), TTL guard (90d), partition by ingest day, no `local_ts` (column doesn't exist on `hft.orders` either — keep schema convention consistent).

---

## Task 3: OrderIntent recorder topic + producer hook + writer — `recorder/worker.py`, `recorder/writer.py`, `strategy/runner.py`

Wire the new topic into the existing `Batcher` pattern AND add the producer hook in `StrategyRunner` so emitted intents actually reach the recorder. Default `HFT_INTENT_RECORDER_ENABLED=0` — never blocks the hot path.

**Files:**
- Modify: `src/hft_platform/recorder/worker.py` (lines ~302-315 topic registration + ~408-455 batchers + new extractor)
- Modify: `src/hft_platform/recorder/writer.py` (insert helper)
- Modify: `src/hft_platform/strategy/runner.py` (~lines 1462-1471, after successful `_risk_submit`/`_risk_submit_typed`) — opt-in `recorder_queue.put_nowait` with drop-on-full
- Test: existing `tests/unit/recorder/test_worker.py` extension (NOT a new file — extend the suite that already exercises the other topics) + `tests/unit/strategy/test_runner_intent_recording.py` for the producer hook

- [ ] **Step 3.1: Define `INTENT_COLUMNS` + extractor**

In `src/hft_platform/recorder/worker.py`, near the existing `MARKET_DATA_COLUMNS`/`ORDER_COLUMNS` constants:

```python
INTENT_COLUMNS = (
    "intent_id", "strategy_id", "symbol", "intent_type", "side",
    "price_scaled", "qty", "tif", "target_order_id",
    "timestamp_ns", "source_ts_ns", "decision_price", "price_type",
    "trace_id", "idempotency_key", "ttl_ns", "reason", "ingest_ts",
)


def _extract_intent_values(row) -> list | None:
    if row is None:
        return None
    intent = row.get("intent") if isinstance(row, dict) else row
    if intent is None:
        return None
    return [
        int(getattr(intent, "intent_id", 0)),
        str(getattr(intent, "strategy_id", "")),
        str(getattr(intent, "symbol", "")),
        getattr(intent.intent_type, "name", str(getattr(intent, "intent_type", ""))),
        getattr(intent.side, "name", str(getattr(intent, "side", ""))),
        int(getattr(intent, "price", 0)),  # scaled int x10000
        int(getattr(intent, "qty", 0)),
        getattr(intent.tif, "name", str(getattr(intent, "tif", ""))),
        str(getattr(intent, "target_order_id", "") or ""),
        int(getattr(intent, "timestamp_ns", 0)),
        int(getattr(intent, "source_ts_ns", 0)),
        int(getattr(intent, "decision_price", 0)),
        str(getattr(intent, "price_type", "LMT")),
        str(getattr(intent, "trace_id", "")),
        str(getattr(intent, "idempotency_key", "")),
        int(getattr(intent, "ttl_ns", 0)),
        str(getattr(intent, "reason", "") or ""),
        int(row.get("ingest_ts") if isinstance(row, dict) else 0),
    ]
```

- [ ] **Step 3.2: Register topic + batcher behind feature flag**

```python
# At ~worker.py:303 _EXTRACTORS dict — add:
_EXTRACTORS["intents"] = _extract_intent_values
_EXTRACTOR_COLUMNS["intents"] = INTENT_COLUMNS

# In RecorderService.__init__, gate on env var:
import os
intent_enabled = os.getenv("HFT_INTENT_RECORDER_ENABLED", "0").lower() in {"1", "true", "yes", "on"}

if intent_enabled:
    self.batchers["intents"] = Batcher(
        "hft.order_intents",
        writer=self.writer,
        extractor=_EXTRACTORS.get("intents"),
        extractor_columns=_EXTRACTOR_COLUMNS.get("intents"),
        memory_guard=self.memory_guard,
        health_tracker=self.health_tracker,
    )
```

**Important:** the hot path emitter MUST use `recorder_queue.put_nowait()` and silently drop on `QueueFull`, mirroring market_data semantics. The recording path MUST NEVER block strategy emission.

- [ ] **Step 3.2b: Producer hook in `strategy/runner.py`**

`StrategyRunner` is where `OrderIntent` instances are constructed (`runner.py:880-895`) and submitted to risk (`runner.py:1462-1471`). To make recording actually happen, wire the opt-in hook *after* a successful risk submit:

```python
# src/hft_platform/strategy/runner.py — inside the submit branch around lines 1462-1471
# After: self._risk_submit(intent) (or _risk_submit_typed)
if self._intent_recorder_enabled:  # cached at __init__ from env var
    try:
        rq = getattr(self, "_recorder_queue", None)
        if rq is not None:
            rq.put_nowait({
                "topic": "intents",
                "data": {"intent": intent, "ingest_ts": timebase.now_ns()},
            })
    except asyncio.QueueFull:
        # silent drop — recorder must never back-pressure the hot path
        self.metrics.recorder_intent_drop_total.inc()
```

In `StrategyRunner.__init__`:

```python
import os
self._intent_recorder_enabled = os.getenv("HFT_INTENT_RECORDER_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
self._recorder_queue = recorder_queue  # injected by services/bootstrap.py same path used for market_data
```

**Bootstrap wiring:** confirm `services/bootstrap.py` already passes `recorder_queue` into `StrategyRunner`. If not, add the kwarg following the existing market_data wiring pattern. Do NOT introduce a new queue — reuse the same one the recorder already drains.

- [ ] **Step 3.3: Writer support — `recorder/writer.py`**

The existing `DataWriter` already handles arbitrary topic names via the `Batcher` interface. **No code change needed** if the existing batcher path is fully topic-agnostic — verify by reading `writer.py` and confirming the topic string is only ever passed through to ClickHouse `INSERT INTO {table}`. If `DataWriter` has a hardcoded topic allowlist, add `intents` there.

- [ ] **Step 3.4: Tests**

Extend `tests/unit/recorder/test_worker.py`:
- `test_intent_topic_disabled_by_default`: env var unset → `"intents" not in svc.batchers`.
- `test_intent_topic_registered_when_enabled`: env var = `"1"` → batcher exists with `table == "hft.order_intents"`.
- `test_extract_intent_values_round_trip`: build an `OrderIntent`, run extractor, assert every numeric/string field round-trips.

New file `tests/unit/strategy/test_runner_intent_recording.py`:
- `test_runner_does_not_record_intents_when_disabled`: env var unset → after one accepted intent, `recorder_queue.qsize() == 0`.
- `test_runner_records_intent_after_successful_submit`: env var = `"1"` → after one submit, queue contains exactly one `{"topic": "intents", ...}` envelope with the same `intent_id`.
- `test_runner_silent_drop_on_recorder_queue_full`: fill the recorder queue to capacity, emit an intent → no exception, `metrics.recorder_intent_drop_total` incremented.

- [ ] **Step 3.5: Verify GREEN + commit**

```bash
uv run pytest tests/unit/recorder/test_worker.py tests/unit/strategy/test_runner_intent_recording.py -q --tb=short
git add src/hft_platform/recorder/worker.py src/hft_platform/recorder/writer.py src/hft_platform/strategy/runner.py tests/unit/recorder/test_worker.py tests/unit/strategy/test_runner_intent_recording.py
git commit -m "feat(recorder): opt-in intents topic + StrategyRunner producer hook (Slice C task 3)"
```

**Verification:** topic disabled by default; `make ci` still green for the rest of the recorder; round-trip extractor preserves `intent_id`, `symbol`, `side`, `price` exactly.

---

## Task 4: Strategy replay harness — `replay/strategy_replay.py`

Drive `RingBufferBus → StrategyRunner.process_event` with a deterministic clock and seeded RNG over a market-data fixture. Capture every emitted `OrderIntent` into a `ReplayedIntentLog`.

**Files:**
- Create: `src/hft_platform/replay/strategy_replay.py`
- Test: `tests/unit/replay/test_strategy_replay.py`

- [ ] **Step 4.1: Public API contract**

```python
@dataclass(frozen=True, slots=True)
class ReplayConfig:
    fixture_path: str               # tar.gz path
    strategy_factory: Any           # callable -> Strategy instance
    symbols: set[str] | None = None
    rng_seed: int = 0
    clock_start_ns: int | None = None  # if None, use first event's exch_ts
    max_events: int | None = None


def replay_strategy(cfg: ReplayConfig) -> "ReplayedIntentLog":
    """Replay market data through a strategy and capture intents.

    Determinism contract:
    - The same (fixture_path, strategy_factory, symbols, rng_seed) MUST
      produce a byte-identical ReplayedIntentLog hash on every invocation.
    """
```

- [ ] **Step 4.2: Failing tests (determinism)**

Tests cover:
- `test_replay_is_deterministic`: two runs with identical config produce identical `log.hash()`.
- `test_replay_captures_intents_in_order`: `intent.timestamp_ns` is monotonic across captured intents.
- `test_replay_respects_max_events`: `max_events=3` halts after 3 events.
- `test_replay_rng_seed_changes_output_when_strategy_uses_rng`: distinct seeds with a randomized strategy produce different hashes.

- [ ] **Step 4.3: Implement**

**Critical determinism contract** (per Codex review):
- `BaseStrategy.handle_event(ctx, event) -> List[OrderIntent]` is **synchronous** at `src/hft_platform/strategy/base.py:266-270`. The harness must NOT await it.
- `StrategyRunner._build_intent` calls `timebase.now_ns()` at `src/hft_platform/strategy/runner.py:889` to stamp `OrderIntent.timestamp_ns`. The harness MUST patch `timebase.now_ns` to return the current event's `exch_ts` so two replays produce byte-identical `timestamp_ns` (and therefore identical `timestamp_us` in the canonical hash).
- `StrategyRunner` also has stale-event logic; the harness must either disable that path or feed events with monotonic `exch_ts` so it does not spuriously drop fixture rows.

```python
# src/hft_platform/replay/strategy_replay.py
from __future__ import annotations
import random
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from hft_platform.contracts.strategy import OrderIntent
from hft_platform.strategy.context import StrategyContext  # adjust import path if needed
from hft_platform.replay.wal_fixture_loader import load_market_data_events
from hft_platform.replay.intent_log import ReplayedIntentLog


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    fixture_path: str
    strategy_factory: Any            # callable(rng=...) -> BaseStrategy instance
    symbols: set[str] | None = None
    rng_seed: int = 0
    clock_start_ns: int | None = None
    max_events: int | None = None


@contextmanager
def _deterministic_clock():
    """Patch timebase.now_ns() so replay-emitted intents are reproducible.

    The clock is updated to the current event's exch_ts before each
    handle_event() call (set by replay_strategy's loop).
    """
    state = {"now": 0}

    def _now():
        return state["now"]

    with patch("hft_platform.timebase.now_ns", side_effect=_now):
        yield state


def replay_strategy(cfg: ReplayConfig) -> ReplayedIntentLog:
    rng = random.Random(cfg.rng_seed)
    log = ReplayedIntentLog()
    strategy = cfg.strategy_factory(rng=rng)
    ctx = StrategyContext()  # minimal; populate symbol map / position store as needed by strategy
    n_events = 0

    with _deterministic_clock() as clock:
        for row in load_market_data_events(cfg.fixture_path, symbols=cfg.symbols):
            if cfg.max_events is not None and n_events >= cfg.max_events:
                break
            exch_ts = int(row.get("exch_ts", 0))
            clock["now"] = cfg.clock_start_ns or exch_ts  # advance the patched clock
            event = _market_data_row_to_event(row)
            intents = strategy.handle_event(ctx, event)  # SYNC, returns List[OrderIntent]
            for intent in intents or []:
                log.append(intent)
            n_events += 1

    log.n_events_processed = n_events
    return log
```

`_market_data_row_to_event` constructs the appropriate event type (BidAskEvent / TickEvent) from the WAL row dict using the existing normalizer schema. **Strategy interface:** the harness calls `strategy.handle_event(ctx, event)` synchronously, matching `src/hft_platform/strategy/base.py:266-270`. No async shim is needed.

**Note on the patch target:** `hft_platform.timebase.now_ns` is the canonical clock. If a strategy uses `time.monotonic_ns` or `time.time_ns` directly (anti-pattern per `01-core-laws.md` §3, but possible in legacy code), Task 4 must additionally patch those names — the test in Step 4.2 (`test_replay_is_deterministic`) will catch any leak by failing to produce identical hashes.

- [ ] **Step 4.4: Verify + commit**

```bash
uv run pytest tests/unit/replay/test_strategy_replay.py -q --tb=short
git add src/hft_platform/replay/strategy_replay.py tests/unit/replay/test_strategy_replay.py
git commit -m "feat(replay): deterministic strategy replay harness (Slice C task 4)"
```

**Verification:** identical config → identical hash; intent ordering matches event ordering; `max_events` bounds processing; RNG seed produces detectably different outputs.

---

## Task 5: Canonical intent log + hash — `replay/intent_log.py`

`ReplayedIntentLog` collects intents and produces a canonical `bytes` form for hashing. The same canonical form is used to hash a "live" intent stream loaded from `hft.order_intents` or a synthetic fixture.

**Files:**
- Create: `src/hft_platform/replay/intent_log.py`
- Test: `tests/unit/replay/test_intent_log.py`

- [ ] **Step 5.1: Define the canonical schema**

Canonical fields, in this order, included in the hash:
- `intent_id`, `strategy_id`, `symbol`
- `intent_type.name`, `side.name`, `tif.name`
- `price` (scaled int), `qty`
- `target_order_id` (or empty string)
- `timestamp_us` (= `timestamp_ns // 1000`, rounded for replay-tolerance)
- `decision_price`, `price_type`

Excluded from hash: `trace_id`, `idempotency_key` (runtime-uniqued), `ttl_ns`, `reason` (free-text), `ingest_ts` (recorder-side), `source_ts_ns` (broker-side).

Rounding to microseconds prevents replay-vs-live drift from sub-µs scheduler jitter while keeping the parity bar tight enough to catch the R47-OE1 cancel-path divergence (which is whole-event-shape, not timing).

- [ ] **Step 5.2: Failing tests**

- `test_canonical_form_excludes_volatile_fields`: two intents differing only in `trace_id` produce identical hash.
- `test_canonical_form_changes_on_price`: intents differing in `price` produce different hashes.
- `test_canonical_form_rounds_timestamp_to_us`: intents at `t` and `t + 500ns` collapse to the same hash.
- `test_load_from_jsonl`: writing one canonical record per line and loading via `from_jsonl` preserves count.

- [ ] **Step 5.3: Implement**

```python
# src/hft_platform/replay/intent_log.py
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _intent_to_canonical(intent: Any) -> dict[str, Any]:
    return {
        "intent_id": int(getattr(intent, "intent_id", 0)),
        "strategy_id": str(getattr(intent, "strategy_id", "")),
        "symbol": str(getattr(intent, "symbol", "")),
        "intent_type": getattr(intent.intent_type, "name", str(intent.intent_type)),
        "side": getattr(intent.side, "name", str(intent.side)),
        "tif": getattr(intent.tif, "name", str(intent.tif)),
        "price": int(getattr(intent, "price", 0)),
        "qty": int(getattr(intent, "qty", 0)),
        "target_order_id": str(getattr(intent, "target_order_id", "") or ""),
        "timestamp_us": int(getattr(intent, "timestamp_ns", 0)) // 1000,
        "decision_price": int(getattr(intent, "decision_price", 0)),
        "price_type": str(getattr(intent, "price_type", "LMT")),
    }


@dataclass
class ReplayedIntentLog:
    intents: list[Any] = field(default_factory=list)
    n_events_processed: int = 0

    def append(self, intent: Any) -> None:
        self.intents.append(intent)

    def n_intents(self) -> int:
        return len(self.intents)

    def canonical_records(self) -> list[dict[str, Any]]:
        return [_intent_to_canonical(it) for it in self.intents]

    def hash(self) -> str:
        h = hashlib.sha256()
        for rec in self.canonical_records():
            h.update(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "ReplayedIntentLog":
        log = cls()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            # Canonical fixtures store timestamp_us (microseconds, rounded).
            # Promote it to timestamp_ns so _intent_to_canonical(...) round-trips
            # to the same bucket: timestamp_us = (timestamp_us * 1000) // 1000.
            if "timestamp_us" in d and "timestamp_ns" not in d:
                d["timestamp_ns"] = int(d.pop("timestamp_us")) * 1000
            # Drop any keys _DictIntent doesn't accept (defensive against
            # canonical-schema additions in future slices).
            allowed = {f.name for f in fields(_DictIntent)}
            d = {k: v for k, v in d.items() if k in allowed}
            log.intents.append(_DictIntent(**d))
        return log


@dataclass
class _DictIntent:
    """Shim for jsonl-loaded intents (mirrors OrderIntent fields by name).

    Canonical fixtures stored on disk use ``timestamp_us`` (microseconds);
    ``from_jsonl`` promotes it to ``timestamp_ns`` before instantiation.
    """
    intent_id: int = 0
    strategy_id: str = ""
    symbol: str = ""
    intent_type: str = "NEW"
    side: str = "BUY"
    tif: str = "LIMIT"
    price: int = 0
    qty: int = 0
    target_order_id: str = ""
    timestamp_ns: int = 0
    decision_price: int = 0
    price_type: str = "LMT"
```

The jsonl loader must add `from dataclasses import fields` to the imports.

- [ ] **Step 5.4: Verify + commit**

```bash
uv run pytest tests/unit/replay/test_intent_log.py -q --tb=short
git add src/hft_platform/replay/intent_log.py tests/unit/replay/test_intent_log.py
git commit -m "feat(replay): canonical intent log with stable hash (Slice C task 5)"
```

---

## Task 6: `IntentDiff` — `alpha/replay_parity.py`

Compute match% and divergence histogram between two intent streams.

**Files:**
- Create: `src/hft_platform/alpha/replay_parity.py`
- Test: `tests/unit/alpha/test_replay_parity.py`

- [ ] **Step 6.1: Failing tests**

- `test_identical_streams_match_100`: identical canonical streams → `match_pct == 100.0`, `first_divergence_idx is None`.
- `test_one_field_diverges_at_idx_5`: change `price` at index 5 → `match_pct == 90.0`, `first_divergence_idx == 5`, `divergence_histogram["price"] >= 1`.
- `test_length_mismatch_handled`: live has 10, replayed has 8 → `divergence_histogram["__missing__"] >= 2`.

- [ ] **Step 6.2: Implement**

```python
# src/hft_platform/alpha/replay_parity.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReplayParityReport:
    match_pct: float
    n_compared: int
    first_divergence_idx: int | None
    divergence_histogram: dict[str, int]
    evidence_path: str
    harness_version: str = "slice-c.v1"


@dataclass
class IntentDiff:
    live: list[dict[str, Any]]
    replayed: list[dict[str, Any]]
    evidence_path: str = ""

    def compute(self) -> ReplayParityReport:
        n_compared = max(len(self.live), len(self.replayed))
        if n_compared == 0:
            return ReplayParityReport(100.0, 0, None, {}, self.evidence_path)
        first_div: int | None = None
        hist: dict[str, int] = {}
        n_match = 0
        for i in range(n_compared):
            a = self.live[i] if i < len(self.live) else None
            b = self.replayed[i] if i < len(self.replayed) else None
            if a is None or b is None:
                hist["__missing__"] = hist.get("__missing__", 0) + 1
                if first_div is None:
                    first_div = i
                continue
            diffs = [k for k in a if a[k] != b.get(k)]
            if not diffs:
                n_match += 1
                continue
            for k in diffs:
                hist[k] = hist.get(k, 0) + 1
            if first_div is None:
                first_div = i
        match_pct = (n_match / n_compared) * 100.0
        return ReplayParityReport(
            match_pct=match_pct,
            n_compared=n_compared,
            first_divergence_idx=first_div,
            divergence_histogram=dict(hist),
            evidence_path=self.evidence_path,
        )
```

- [ ] **Step 6.3: Verify + commit**

```bash
uv run pytest tests/unit/alpha/test_replay_parity.py -q --tb=short
git add src/hft_platform/alpha/replay_parity.py tests/unit/alpha/test_replay_parity.py
git commit -m "feat(alpha): IntentDiff + ReplayParityReport (Slice C task 6)"
```

---

## Task 7: Synthetic R47-OE1 divergence fixture

Path-(b) evidence: hand-constructed live + replayed intent streams that simulate the R47-OE1 cancel-path short-circuit. These are static jsonl fixtures committed to `tests/fixtures/replay_parity/`.

**Files:**
- Create: `tests/fixtures/replay_parity/synthetic_r47_oe1_divergence.json` (manifest)
- Create: `tests/fixtures/replay_parity/synthetic_r47_oe1_live.jsonl`
- Create: `tests/fixtures/replay_parity/synthetic_r47_oe1_replayed.jsonl`
- Create: `tests/fixtures/replay_parity/clean_echo_live.jsonl`
- Create: `tests/fixtures/replay_parity/clean_echo_replayed.jsonl`

- [ ] **Step 7.1: Write the manifest**

```json
{
  "fixture_set": "replay_parity",
  "version": 1,
  "scenarios": [
    {
      "id": "r47_oe1_cancel_short_circuit",
      "live_jsonl": "synthetic_r47_oe1_live.jsonl",
      "replayed_jsonl": "synthetic_r47_oe1_replayed.jsonl",
      "expected_match_pct_max": 95.0,
      "rationale": "Live stream omits ~6% of CANCEL intents due to OE1 cancel-path short-circuit observed 2026-04-21. Replay (with fix) emits them, so live<replayed by ~6 intents in the divergence histogram under '__missing__'."
    },
    {
      "id": "clean_1tick_echo",
      "live_jsonl": "clean_echo_live.jsonl",
      "replayed_jsonl": "clean_echo_replayed.jsonl",
      "expected_match_pct_min": 100.0,
      "rationale": "Deterministic 1-tick echo; live and replayed should match byte-for-byte under canonical hashing."
    }
  ]
}
```

- [ ] **Step 7.2: Generate the synthetic R47 streams**

Author 100 intents per stream. The "live" stream omits 6 CANCEL intents at deterministic positions (e.g. indices 12, 24, 36, 48, 60, 72) while the "replayed" stream includes all 100. All other fields identical. This produces match_pct ≈ 94% and divergence_histogram = {"__missing__": 6}.

**Important:** use the same canonical schema that `_intent_to_canonical()` produces (Task 5) so a downstream loader can read the fixtures via `ReplayedIntentLog.from_jsonl()` without translation.

- [ ] **Step 7.3: Generate the clean echo streams**

100 NEW intents that match byte-for-byte across the two files. Used to verify the gate is not always-fail (DoD-C2).

- [ ] **Step 7.4: Round-trip test**

Extend `tests/unit/alpha/test_replay_parity.py` with two fixture-driven cases:
- `test_r47_oe1_synthetic_fixture_below_threshold`: load both jsonls via `ReplayedIntentLog.from_jsonl`, run `IntentDiff.compute()`, assert `match_pct < 95.0`.
- `test_clean_echo_fixture_at_100`: same flow, assert `match_pct == 100.0`.

- [ ] **Step 7.5: Commit**

```bash
git add tests/fixtures/replay_parity/
git commit -m "feat(test): synthetic R47-OE1 + clean echo replay parity fixtures (Slice C task 7)"
```

---

## Task 8: `ReplayParityGate` sub-gate

Pure callable. Reads `result.replay_parity_report` and the threshold from `vm_ul6_strict.yaml`.

**Files:**
- Create: `src/hft_platform/alpha/_sub_gates/replay_parity.py`
- Test: `tests/unit/alpha/test_sub_gate_replay_parity.py`

- [ ] **Step 8.1: Failing tests**

- `test_passes_when_match_pct_above_threshold`: `match_pct=96.0` vs threshold `95.0` → `passed=True`.
- `test_fails_when_below_threshold`: `match_pct=80.0` → `passed=False`, `metrics["first_divergence_idx"]==12`.
- `test_missing_report_marks_gate_failed`: `replay_parity_report=None` → `passed=False`, details mention "missing".

- [ ] **Step 8.2: Implement**

```python
# src/hft_platform/alpha/_sub_gates/replay_parity.py
from __future__ import annotations
from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


class ReplayParityGate:
    name = "replay_parity"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        threshold = float(thresholds.get("replay_parity_match_pct_min", 95.0))
        report = getattr(result, "replay_parity_report", None)
        if report is None:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={"match_pct": 0.0, "threshold": threshold},
                details="missing replay_parity_report — required under strict profile",
            )
        match_pct = float(getattr(report, "match_pct", 0.0))
        passed = match_pct >= threshold
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "match_pct": match_pct,
                "n_compared": float(getattr(report, "n_compared", 0)),
                "first_divergence_idx": float(getattr(report, "first_divergence_idx", -1) or -1),
                "threshold": threshold,
            },
            details=(
                f"match_pct={match_pct:.2f} vs min {threshold:.2f}"
                + (f"; first divergence at idx {report.first_divergence_idx}" if not passed and getattr(report, "first_divergence_idx", None) is not None else "")
            ),
        )
```

- [ ] **Step 8.3: Verify + commit**

```bash
uv run pytest tests/unit/alpha/test_sub_gate_replay_parity.py -q --tb=short
git add src/hft_platform/alpha/_sub_gates/replay_parity.py tests/unit/alpha/test_sub_gate_replay_parity.py
git commit -m "feat(alpha): ReplayParityGate sub-gate (Slice C task 8)"
```

---

## Task 9: Register sub-gate + extend strict profile

Wire the new gate into `ensure_builtin_sub_gates_registered()` and add it to `vm_ul6_strict.yaml :: blocking_sub_gates`.

**Files:**
- Modify: `src/hft_platform/alpha/_sub_gates/__init__.py`
- Modify: `config/research/profiles/vm_ul6_strict.yaml`

- [ ] **Step 9.1: Register**

```python
# src/hft_platform/alpha/_sub_gates/__init__.py inside ensure_builtin_sub_gates_registered():
from hft_platform.alpha._sub_gates.replay_parity import ReplayParityGate
# ... in the existing chain that registers each gate by name ...
if "replay_parity" not in existing_names:
    register_sub_gate(ReplayParityGate())
```

- [ ] **Step 9.2: Extend YAML profile**

`ReplayParityGate.applies_to = {"maker", "taker"}`, so the threshold MUST exist under both threshold sections — otherwise taker promotion silently falls back to the gate's `95.0` literal default.

```yaml
# config/research/profiles/vm_ul6_strict.yaml — under taker thresholds:
    replay_parity_match_pct_min: 95.0

# AND under maker thresholds:
    replay_parity_match_pct_min: 95.0

# blocking_sub_gates: append
  - replay_parity
```

- [ ] **Step 9.3: Tests**

- `test_strict_profile_includes_replay_parity` in `tests/unit/alpha/test_validation_profile.py`: load the profile and assert `"replay_parity" in profile.blocking_sub_gates`.
- `test_replay_parity_auto_registered` in `tests/unit/alpha/test_sub_gate_replay_parity.py`: call `ensure_builtin_sub_gates_registered()` and assert the name is in the registry.

- [ ] **Step 9.4: Verify + commit**

```bash
uv run pytest tests/unit/alpha/test_validation_profile.py tests/unit/alpha/test_sub_gate_replay_parity.py -q --tb=short
git add src/hft_platform/alpha/_sub_gates/__init__.py config/research/profiles/vm_ul6_strict.yaml tests/unit/alpha/
git commit -m "feat(alpha): register replay_parity in strict profile (Slice C task 9)"
```

---

## Task 10: Gate D `replay_parity_audit` — `promotion.py`

Mirror the existing `latency_profile` check at `src/hft_platform/alpha/promotion.py:315-320`. Promotion-time check that the scorecard recorded a passing `replay_parity_report` under a strict profile.

**Files:**
- Modify: `src/hft_platform/alpha/promotion.py`
- Test: `tests/unit/test_alpha_promotion.py`

- [ ] **Step 10.1: Failing test**

- `test_promotion_blocks_when_replay_parity_below_threshold`: scorecard with `replay_parity.match_pct = 80.0` and `PromotionConfig.min_replay_parity_match_pct = 95.0` → `_evaluate_gate_d` returns `passed=False` and `checks["replay_parity_audit"]["pass"] is False`.
- `test_promotion_passes_when_replay_parity_at_or_above_threshold`: `match_pct = 96.0` → `checks["replay_parity_audit"]["pass"] is True`.

- [ ] **Step 10.2: Implement**

```python
# src/hft_platform/alpha/promotion.py inside _evaluate_gate_d, after latency_profile block:
replay_parity = scorecard.get("replay_parity") or None
match_pct = None
if isinstance(replay_parity, dict):
    match_pct = _to_float(replay_parity.get("match_pct"))

checks["replay_parity_audit"] = {
    "value": match_pct,
    "min": getattr(config, "min_replay_parity_match_pct", 95.0),
    "required": True,
    "pass": (match_pct is not None and match_pct >= getattr(config, "min_replay_parity_match_pct", 95.0)),
    "detail": (
        "OK"
        if match_pct is not None
        else "MISSING — scorecard.replay_parity must be populated before promotion"
    ),
}
```

Add `min_replay_parity_match_pct: float = 95.0` to `PromotionConfig` (in the same file or its `_promotion_config.py` module).

- [ ] **Step 10.3: Verify + commit**

```bash
uv run pytest tests/unit/test_alpha_promotion.py -q --tb=short
git add src/hft_platform/alpha/promotion.py tests/unit/test_alpha_promotion.py
git commit -m "feat(alpha): Gate D replay_parity_audit check (Slice C task 10)"
```

---

## Task 11: Wire `replay_parity_report` through `BacktestResult`

`_invoke_sub_gates()` (Slice A's aggregator) needs a `BacktestResult` that carries the parity report. Add an optional field.

**Files:**
- Modify: `src/hft_platform/alpha/_validation_types.py`
- Test: existing `tests/unit/alpha/test_validation_types.py` extension

- [ ] **Step 11.1: Add field**

```python
# src/hft_platform/alpha/_validation_types.py
@dataclass(slots=True)
class BacktestResult:
    # ... existing fields ...
    replay_parity_report: Any | None = None
```

- [ ] **Step 11.2: Test**

Assert that constructing `BacktestResult(daily_pnl=[1.0], replay_parity_report=<report>)` round-trips the field without coercion.

- [ ] **Step 11.3: Commit**

```bash
git add src/hft_platform/alpha/_validation_types.py tests/unit/alpha/test_validation_types.py
git commit -m "feat(alpha): BacktestResult.replay_parity_report optional field (Slice C task 11)"
```

---

## Task 12: Integration test — synthetic R47 KILL (DoD-C1)

End-to-end: run the harness against the synthetic fixture, build a scorecard with the resulting `replay_parity_report`, attempt promotion under `vm_ul6_strict`, expect Gate-C `replay_parity` to FAIL.

**Files:**
- Create: `tests/integration/test_replay_parity_e2e.py`

- [ ] **Step 12.1: Test**

The DoD-C1 test must exercise the **profile aggregation path** (`_invoke_sub_gates`), not the gate in isolation — otherwise the test would pass even if the gate were never registered or never wired into `blocking_sub_gates`. Use the keyword-argument signature at `src/hft_platform/alpha/_gate_c.py:48-55` and assert against `blocking["passed"]`. Mirror the pattern in `tests/integration/test_strict_profile_e2e.py:98-105`.

```python
import pytest
from pathlib import Path

from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import load_profile
from hft_platform.alpha.replay_parity import IntentDiff
from hft_platform.replay.intent_log import ReplayedIntentLog

FIXTURE_DIR = Path("tests/fixtures/replay_parity")


@pytest.mark.integration
def test_dod_c1_synthetic_r47_kills_at_replay_parity_gate():
    """DoD-C1: R47-OE1 fingerprint produces Gate-C replay_parity FAIL at >=5% divergence."""
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "synthetic_r47_oe1_live.jsonl")
    rep  = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "synthetic_r47_oe1_replayed.jsonl")
    report = IntentDiff(live=live.canonical_records(), replayed=rep.canonical_records()).compute()
    assert report.match_pct < 95.0  # below threshold

    profile = load_profile("config/research/profiles/vm_ul6_strict.yaml")
    payload = _r47_payload_with(replay_parity_report=report)  # build a result_payload dict
    advisory, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=payload,
        thresholds=profile.thresholds["maker"],
        profile=profile,
    )
    assert blocking is not None
    assert blocking["passed"] is False
    assert any(g["name"] == "replay_parity" for g in blocking["failing"])


@pytest.mark.integration
def test_dod_c2_clean_echo_passes():
    """DoD-C2: deterministic 1-tick echo produces 100% parity AND blocking aggregate passes."""
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "clean_echo_live.jsonl")
    rep  = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "clean_echo_replayed.jsonl")
    report = IntentDiff(live=live.canonical_records(), replayed=rep.canonical_records()).compute()
    assert report.match_pct == 100.0

    profile = load_profile("config/research/profiles/vm_ul6_strict.yaml")
    payload = _clean_payload_with(replay_parity_report=report)
    advisory, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=payload,
        thresholds=profile.thresholds["maker"],
        profile=profile,
    )
    # replay_parity itself passes; other strict gates may fail on the synthetic
    # payload — assert specifically that replay_parity is not in failing.
    assert blocking is not None
    failing_names = {g["name"] for g in blocking["failing"]}
    assert "replay_parity" not in failing_names
```

Helper builders `_r47_payload_with` and `_clean_payload_with` produce a `result_payload` dict that satisfies the rest of the strict gates (>= `min_fills`, `min_days`, etc.) so the only gate exercised is `replay_parity`. Mirror `_r47_payload()` in `tests/integration/test_strict_profile_e2e.py`.

- [ ] **Step 12.2: Verify + commit**

```bash
uv run pytest tests/integration/test_replay_parity_e2e.py -q --tb=short
git add tests/integration/test_replay_parity_e2e.py
git commit -m "test(alpha): DoD-C1+C2 replay parity end-to-end (Slice C task 12)"
```

---

## Task 13: Loose-profile parity — non-regression

Verify the gate is **not** blocking for loose `make research` runs. Mirrors Slice A's loose-parity test.

**Files:**
- Extend: `tests/integration/test_replay_parity_e2e.py`

- [ ] **Step 13.1: Test**

Mirror `tests/integration/test_strict_profile_e2e.py::test_loose_profile_preserves_advisory_only_behavior`:

```python
@pytest.mark.integration
def test_loose_profile_does_not_block_on_replay_parity():
    """Without strict profile, replay_parity gate stays advisory and blocking is None."""
    from hft_platform.alpha._gate_c import _invoke_sub_gates
    payload = _r47_payload_with(replay_parity_report=None)  # missing report
    advisory, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=payload,
        thresholds={"sharpe_is_min": 0.5, "winning_day_pct_min": 55, "replay_parity_match_pct_min": 95.0},
        profile=None,
    )
    assert blocking is None  # no profile → no blocking aggregate
    # replay_parity is still in advisory list (registered)
    assert any(g["name"] == "replay_parity" for g in advisory)
```

- [ ] **Step 13.2: Commit**

```bash
git commit -m "test(alpha): replay_parity stays advisory under loose profile (Slice C task 13)"
```

---

## Task 14: Path-(a) live reconstruction — USER-AUTHORIZATION GATED

Reconstruct intents from `hft.orders` `OrderCommand` rows for 2026-04-21. **Requires the user to explicitly authorize a one-time CK read.** If the user declines, this task is skipped and the synthetic fixture from Task 7 satisfies DoD-C1.

- [ ] **Step 14.1: Ask the user**

Surface the question:

> "Path (a) of DoD-C1 needs a one-time read of `hft.orders` rows for `toDate(toDateTime64(ingest_ts/1e9,3)) = '2026-04-21'`. This pulls live trading data into the test fixture. Authorize? (yes / no / skip-and-rely-on-synthetic)"

If `no` or `skip`: mark Task 14 done with rationale and proceed to Task 15.

- [ ] **Step 14.2: Export script (only if authorized)**

```python
# scripts/export_2026_04_21_intents.py
"""One-time export of OrderCommand rows from hft.orders for 2026-04-21
into a tar.gz fixture suitable for IntentDiff path-(a)."""
# Reads hft.orders, projects {strategy_id, symbol, side, price_scaled, qty,
# order_id, ingest_ts, oc_type, client_order_id} for 2026-04-21,
# rebuilds canonical intent records (intent_type from oc_type), writes to
# tests/fixtures/replay_parity/live_2026_04_21_intents.jsonl.
```

- [ ] **Step 14.3: Add fixture to manifest, repeat Task 12 with path-(a) data**

If path (a) data is available, add a third scenario to the manifest:

```json
{
  "id": "live_2026_04_21",
  "live_jsonl": "live_2026_04_21_intents.jsonl",
  "replayed_jsonl": "replay_2026_04_21_intents.jsonl",
  "expected_match_pct_max": 95.0,
  "rationale": "Live 2026-04-21 R47-OE1 session vs offline replay of same WAL feed; expected divergence reflects the cancel-path short-circuit fixed in Slice A era."
}
```

The "replayed" file is generated by running the harness (Task 4) against the WAL fixture (`~/.local/share/hft-fixtures/wal_2026_04_21.tar.gz`) using a stand-in R47 strategy factory.

- [ ] **Step 14.4: Commit (path-(a) only)**

```bash
git add scripts/export_2026_04_21_intents.py tests/fixtures/replay_parity/synthetic_r47_oe1_divergence.json tests/fixtures/replay_parity/live_2026_04_21_intents.jsonl
git commit -m "feat(replay): live 2026-04-21 path-(a) fixture (Slice C task 14, user-authorized)"
```

If skipped: commit a `tests/fixtures/replay_parity/SKIPPED_PATH_A.md` documenting the decision and citing the date the user declined.

---

## Task 15: Documentation + cold-start verification (DoD-C4)

A fresh subagent must be able to run the full Slice C flow from this plan + the repo. Verify by handing off to a new subagent and watching it execute Task 1.

**Files:**
- Modify: `docs/architecture/current-architecture.md` (add `replay/` package + new sub-gate to architecture map)
- Create: `docs/runbooks/replay-parity-gate.md` (operator runbook)

- [ ] **Step 15.1: Write runbook**

Sections required:
- Why this gate exists (R47-OE1 incident summary, link to `docs/incidents/2026-04-21-r47-backtest-live-divergence.md`)
- How to enable intent recording (env var, default-off rationale)
- How to read `hft.order_intents` for a session
- How to run the offline replay harness for a recorded session
- How to read a `divergence_histogram` (canonical fields, what `__missing__` means)
- Threshold configuration (`replay_parity_match_pct_min` in `vm_ul6_strict.yaml`)

- [ ] **Step 15.2: Architecture map update**

Add the new module list under "Slice C deliverables" in `current-architecture.md`.

- [ ] **Step 15.3: Cold-start subagent verification**

Hand a fresh subagent this plan + the repo with no other context. The subagent must be able to execute Task 1 end-to-end (loader + tests + green) using only the plan as guidance. If the subagent gets stuck on a missing context cue, edit the plan to add the missing detail before merging.

- [ ] **Step 15.4: Commit**

```bash
git add docs/runbooks/replay-parity-gate.md docs/architecture/current-architecture.md
git commit -m "docs(replay): runbook + architecture map for parity gate (Slice C task 15)"
```

---

## Task 16: Final verification + plan close-out

- [ ] **Step 16.1: Full `make ci` green**

```bash
make ci
# Expected: format-check + lint + typecheck + coverage all green.
# Domain coverage floors must be met (live money domains untouched).
```

- [ ] **Step 16.2: Coverage floor check**

```bash
uv run pytest --cov=hft_platform.alpha._sub_gates.replay_parity \
  --cov=hft_platform.alpha.replay_parity \
  --cov=hft_platform.replay \
  --cov-report=term-missing -q
# Slice C new code MUST be ≥80% line coverage; ≥90% on the gate path.
```

- [ ] **Step 16.3: Self-review checklist (next section)**

- [ ] **Step 16.4: Open PR**

```bash
git push -u origin slice-c/replay-parity-gate
gh pr create --title "Slice C — Replay-diff parity gate (vm_ul6_strict + sub-gate + Gate D audit)" --body "$(cat <<'EOF'
## Goal

Make Gate C / Gate D refuse alphas whose live OrderIntent stream diverges from an offline replay of the same market input. Default to a synthetic R47-OE1 divergence fixture; expose path-(a) live reconstruction as a user-gated follow-up.

## Why

R47 incident 2026-04-21: live PnL −1,722 NTD vs +7,701 NTD backtest. Cancel-path short-circuit, invisible to existing gates. Slice C is the parity guard.

## What changed

- New opt-in `intents` recorder topic (`HFT_INTENT_RECORDER_ENABLED=0` by default) → `hft.order_intents`
- New `replay/strategy_replay.py` deterministic harness
- New `alpha/replay_parity.py` IntentDiff
- New `alpha/_sub_gates/replay_parity.py` ReplayParityGate
- `vm_ul6_strict.yaml` adds `replay_parity` to `blocking_sub_gates`
- `promotion.py :: _evaluate_gate_d` adds `replay_parity_audit` mirroring `latency_profile`

## Test plan

- [x] DoD-C1 — synthetic R47-OE1 fixture KILLs Gate-C replay_parity (≥5% divergence)
- [x] DoD-C2 — deterministic 1-tick echo PASSes at 100%
- [x] DoD-C3 — `make ci` green; 80%+ coverage
- [x] DoD-C4 — cold-start subagent ran Task 1 successfully
- [ ] DoD-C1 path (a) — live 2026-04-21 reconstruction (user-gated; SKIPPED with rationale documented in fixtures/SKIPPED_PATH_A.md unless user authorizes Task 14)

## Out of scope

- Slice B (MakerEngine residual MtM) — separate slice, dependency only as a process guard
- Slice D (alpha factory MVP) — depends on this slice
- DSL-driven replay strategy authoring — Slice D
EOF
)"
```

- [ ] **Step 16.5: Tag close-out**

```bash
git tag slice-c-merged-$(date +%Y-%m-%d)
echo "Slice C merged. Update MEMORY.md with this slice's summary card. Phase 2 (Slice B) starts after this PR is merged + main is clean."
```

---

## Self-review

Before opening the PR, verify:

- [ ] **Spec coverage:** every locked decision (1-6) from the master blueprint maps to at least one task above.
- [ ] **Type/name consistency:** new types follow existing naming (`SubGateResult`, `BacktestResult`, `ValidationProfile`); no `XxxResultV2` or `*_new` suffixes.
- [ ] **No placeholders:** every code block is concrete; no `# TODO: implement` placeholders shipped.
- [ ] **Out-of-scope drift:** no MakerEngine changes (Slice B), no DSL/screener (Slice D), no float→int conversions in live money domains.
- [ ] **HFT-P004 compliance:** `OrderIntent.price` and `qty` stay as scaled int through the canonical form. `match_pct` is a percentage (legitimate float per `25-architecture-governance.md` §11 — research path only, never live money).
- [ ] **Coverage ratchet (PR #328):** new code coverage meets domain floors; live money paths untouched.
- [ ] **No `--no-verify` / `--no-gpg-sign`:** all commits pass pre-commit hooks.

---

## Execution handoff

**Recommended path: subagent-driven development** (`superpowers:subagent-driven-development`).

For each task:
1. Spawn a fresh subagent with **only**: this plan file path + the task number + the repo root.
2. Subagent executes RED → GREEN → REFACTOR → commit cycle as written.
3. Two-stage review between tasks: a code-reviewer subagent inspects the commit, a python reviewer flags style/perf issues, then merge to the slice branch.
4. After Task 16, the slice branch opens its single PR.

**Inherited Slice A patterns enforced here:**

- Each task is self-contained: a fresh subagent picking up Task N can complete it from this plan alone.
- `make ci` is the gate — no `--no-verify`, no `--no-gpg-sign`.
- Strict profile YAML changes (Task 9) are the only path that turns `replay_parity` from advisory to blocking.
- The runtime intent recorder (Task 3) is opt-in and never blocks the hot path.
- Coverage floors apply via the same `scripts/check_coverage_domains.py` ratchet.

**Codex adversarial review applied 2026-05-04** (verdict: ACCEPT-WITH-FIXES). 5 HIGH and 3 MEDIUM findings folded into this plan:

1. (HIGH) `BaseStrategy.handle_event(ctx, event)` is **synchronous**; harness must not await — folded into Task 4.
2. (HIGH) `StrategyRunner._build_intent` calls `timebase.now_ns()` at `runner.py:889`; harness must patch the clock to event `exch_ts` for determinism — folded into Task 4.
3. (HIGH) No producer hook exists today; `StrategyRunner` must emit to `recorder_queue.put_nowait` after `_risk_submit` — folded into Task 3 Step 3.2b.
4. (HIGH) Test code used `loose_config()` (does not exist) and wrong positional `_invoke_sub_gates(...)` signature — folded into Tasks 12 & 13 with the keyword-arg pattern from `tests/integration/test_strict_profile_e2e.py`.
5. (HIGH) DoD-C1 originally bypassed profile aggregation by calling the gate directly — Task 12 now exercises `_invoke_sub_gates(profile=strict_profile)` and asserts `blocking["passed"] is False`.
6. (MEDIUM) Migration filename convention is `YYYYMMDD_NNN_*.sql`, not `NNNN_*.sql` — Task 2 renamed.
7. (MEDIUM) `_DictIntent` did not accept `timestamp_us` from canonical jsonl — Task 5 jsonl loader now promotes it and filters unknown fields.
8. (MEDIUM) `ReplayParityGate.applies_to = {"maker","taker"}` but the threshold lived only under maker — Task 9 adds it under both.

LOW findings: file:line citations re-verified clean (`OrderIntent` at `contracts/strategy.py:35-36`, recorder topics at `worker.py:302-315`, `_evaluate_gate_d` at `promotion.py:278`). Governance compliance (HFT-P004, MB-04, coverage floor) verified clean. No out-of-scope drift into Slice B/D files.
