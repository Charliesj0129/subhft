# E2E Pipeline Tests — 7-Plane Complete Chain Verification

Date: 2026-04-05
Status: Approved
Scope: End-to-end test suite covering all 7 runtime planes of the HFT platform.

## 1. Goal

Create a unified E2E test suite under `tests/e2e/` that systematically verifies every runtime plane's canonical data flow from input to output. Each plane gets two tiers of coverage:

- **Chain tests** — lightweight, fast, verify the plane's core transform with minimal wiring.
- **Integration tests** — wire real service instances as async tasks, verify cross-queue handoffs and supervision behavior.

## 2. Structure

```
tests/e2e/
├── conftest.py                            # Shared fixtures
├── test_01_control_plane.py               # Config → Bootstrap → ServiceRegistry
├── test_02_market_data_plane.py           # BrokerCallback → Normalize → LOB → Feature → Bus
├── test_03_decision_plane.py              # BusEvent → Strategy → Risk → OrderCommand
├── test_04_execution_plane.py             # OrderCommand → Broker → ExecRouter → Position
├── test_05_persistence_plane.py           # RecorderQueue → Batcher → Writer / WAL
├── test_06_observability_safety_plane.py  # Metrics + StormGuard + Supervisor HALT
├── test_07_alpha_governance_plane.py      # Gate A → B → C → D → E → Canary
```

## 3. Design Decisions

### 3.1 Rust-Enabled by Default

Tests import `hft_platform.rust_core` at module level via `pytest.importorskip`. This tests the actual production code path. CI environments without Rust built skip gracefully.

### 3.2 No External Dependencies

- ClickHouse: mocked via `MagicMock` writer in persistence tests.
- WAL: uses `tmp_path` for file I/O.
- Redis: not required.
- Docker: not required.
- Broker SDK: `InMemoryBrokerAPI` replaces real client.

### 3.3 Async Timeout Discipline

Integration tests use `asyncio.wait_for(timeout=5.0)` to prevent hangs. Chain tests are synchronous where possible.

### 3.4 Test Markers

```python
@pytest.mark.e2e              # Entire suite
@pytest.mark.e2e_chain        # Lightweight chain tests only
@pytest.mark.e2e_integration  # Full wired integration tests only
```

Usage: `pytest tests/e2e/ -m e2e_chain` for fast CI, `pytest tests/e2e/` for full suite.

### 3.5 Naming Convention

Per project rule (`.agent/rules/50-testing.md`): `test_<behavior>_<scenario>`.

## 4. Shared Fixtures (`tests/e2e/conftest.py`)

### 4.1 InMemoryBrokerAPI

A single mock broker class shared across all planes. Richer than existing `MockBroker`:

```python
class InMemoryBrokerAPI:
    """API-compatible in-memory broker for E2E tests."""
    placed_orders: list       # Tracks all placed orders
    cancelled_orders: list    # Tracks all cancellations
    fill_queue: asyncio.Queue # Generated fills for injection into raw_exec_queue
    should_reject: bool       # Toggle to simulate broker rejection

    def place_order(self, **kwargs) -> dict: ...
    def cancel_order(self, trade, **kwargs) -> dict: ...
    def update_order(self, trade, **kwargs) -> dict: ...
    def get_exchange(self, symbol: str) -> str: ...
```

### 4.2 Bounded Queues

```python
@pytest.fixture
def bounded_queues() -> dict[str, asyncio.Queue]:
    """All 5 runtime queues with small bounds (64) for fast timeout detection."""
    return {
        "raw_queue": asyncio.Queue(maxsize=64),
        "raw_exec_queue": asyncio.Queue(maxsize=64),
        "risk_queue": asyncio.Queue(maxsize=64),
        "order_queue": asyncio.Queue(maxsize=64),
        "recorder_queue": asyncio.Queue(maxsize=64),
    }
```

### 4.3 Wired Bus

```python
@pytest.fixture
def wired_bus() -> RingBufferBus:
    """RingBufferBus with overflow → StormGuard HALT wiring."""
```

### 4.4 E2E Settings

```python
@pytest.fixture
def e2e_settings(tmp_path) -> dict:
    """Minimal Settings with symbols.yaml + strategy_limits.yaml."""
```

### 4.5 Reused Factories

Import from existing `tests/factories/`:
- `make_tick_event`, `make_bidask_event`, `make_lob_stats_event`, `make_fill_event`
- `make_order_intent`, `make_order_command`

## 5. Per-Plane Specification

### 5.1 Plane 1: Control Plane (`test_01_control_plane.py`)

**Chain tests:**

| Test | Input | Assert |
|------|-------|--------|
| `test_config_merge_priority` | base YAML + env YAML + env vars + CLI overrides | Final settings reflect correct priority: CLI > env var > env YAML > base YAML |
| `test_symbols_yaml_loading` | `symbols.yaml` with 3 symbols | `SymbolMetadata` has correct scale factors and exchange mappings |
| `test_env_mode_resolution` | `HFT_MODE=sim` env var | Settings mode is `sim`, order mode is `sim` |

**Integration tests:**

| Test | Setup | Assert |
|------|-------|--------|
| `test_bootstrap_builds_valid_registry` | `SystemBootstrapper(settings).build()` with mocked heavy constructors | All queues bounded and non-None; all services instantiated; StormGuard wired to bus |
| `test_bootstrap_queue_bounds_enforced` | Set `HFT_RAW_QUEUE_SIZE=10` (below minimum 1024) | Queue size clamped to 1024 |
| `test_bootstrap_feature_engine_wiring` | `HFT_FEATURE_ENGINE_ENABLED=1` | `FeatureEngine` present in registry, wired to `MarketDataService` |

### 5.2 Plane 2: Market Data Plane (`test_02_market_data_plane.py`)

**Chain tests:**

| Test | Input | Assert |
|------|-------|--------|
| `test_tick_normalization_scaled_int` | Raw Shioaji tick dict (`price=100.5`) | `TickEvent.price == 1005000` (x10000) |
| `test_bidask_normalization_book_shape` | Raw Shioaji bidask dict (5-level) | `BidAskEvent.bids.shape == (5, 2)`, dtype `int64`, prices scaled |
| `test_lob_engine_stats_computation` | Normalized `BidAskEvent` | `LOBStatsEvent` with valid `mid_price_x2`, `spread_scaled`, `imbalance` |
| `test_feature_engine_27_features` | `LOBStatsEvent` + `TickEvent` sequence | Feature array has 27 populated slots (v3 schema) |
| `test_normalize_to_feature_full_chain` | Raw tick dict → normalize → LOB → feature | End-to-end: raw dict in, 27-feature array out |

**Integration tests:**

| Test | Setup | Assert |
|------|-------|--------|
| `test_md_service_publishes_to_bus` | `MarketDataService.run()` as async task; inject raw tick into `raw_queue` | `TickEvent` appears on `RingBufferBus` within 2s |
| `test_md_service_bidask_lob_chain` | Inject raw bidask into `raw_queue` | `BidAskEvent` + `LOBStatsEvent` both appear on bus |
| `test_md_service_recorder_direct_write` | Inject raw tick | `recorder_queue` receives mapped record (bypasses bridge) |

### 5.3 Plane 3: Decision Plane (`test_03_decision_plane.py`)

**Chain tests:**

| Test | Input | Assert |
|------|-------|--------|
| `test_strategy_emits_intent` | `LOBStatsEvent` → mock strategy's `handle_event()` | Returns `OrderIntent` with correct symbol, price (scaled int), side |
| `test_risk_approve_valid_intent` | Valid `OrderIntent` → `RiskEngine.evaluate()` | `RiskDecision.approved == True`, `OrderCommand` has `deadline_ns` set |
| `test_risk_reject_halt_state` | `StormGuard` in HALT + non-exempt `OrderIntent` | `RiskDecision.approved == False`, reason contains "halt" |
| `test_risk_reject_exposure_limit` | Intent exceeding position limit | `RiskDecision.approved == False` |

**Integration tests:**

| Test | Setup | Assert |
|------|-------|--------|
| `test_strategy_to_risk_queue` | `StrategyRunner` + `RiskEngine` as async tasks; publish `LOBStatsEvent` to bus | `OrderCommand` arrives on `order_queue` within 3s |
| `test_rejection_does_not_reach_order_queue` | Strategy emits intent that violates risk limit | `order_queue` remains empty after 1s; rejection logged |
| `test_gateway_path_intent_to_command` | Enable `GatewayService`; strategy emits intent via `intent_channel` | `OrderCommand` arrives on `order_queue` (routed through gateway → risk) |

### 5.4 Plane 4: Execution Plane (`test_04_execution_plane.py`)

**Chain tests:**

| Test | Input | Assert |
|------|-------|--------|
| `test_order_adapter_calls_broker` | `OrderCommand` → `OrderAdapter.execute()` | `InMemoryBrokerAPI.placed_orders` has 1 entry with correct symbol/price/qty |
| `test_execution_router_normalizes_fill` | Raw exec event dict → `ExecutionRouter` normalize | `FillEvent` with scaled int price, correct `fee`/`tax` |
| `test_position_store_updates_on_fill` | `FillEvent(BUY, qty=2)` → `PositionStore.on_fill()` | `net_qty == 2`, `avg_price` correct |
| `test_full_execution_chain` | `OrderCommand` → adapter → mock fill → router → position | `PositionDelta` with `net_qty` and `realized_pnl` correct |

**Integration tests:**

| Test | Setup | Assert |
|------|-------|--------|
| `test_order_to_fill_async_pipeline` | `OrderAdapter` + `ExecutionRouter` as async tasks; inject `OrderCommand` to `order_queue`; broker generates fill into `raw_exec_queue` | `FillEvent` + `PositionDelta` appear on `RingBufferBus` |
| `test_cancel_order_flow` | Place order, then inject cancel `OrderCommand` | `InMemoryBrokerAPI.cancelled_orders` has 1 entry; `OrderEvent(CANCELLED)` on bus |
| `test_broker_reject_triggers_dlq` | `InMemoryBrokerAPI.should_reject = True`; inject `OrderCommand` | Order enters DLQ; `order_reject_total` metric incremented |

### 5.5 Plane 5: Persistence Plane (`test_05_persistence_plane.py`)

**Chain tests:**

| Test | Input | Assert |
|------|-------|--------|
| `test_batcher_flush_on_threshold` | Add records until batch threshold → `check_flush()` | `DataWriter.insert()` called with correct table and row count |
| `test_wal_write_and_read_roundtrip` | `WALWriter.write(records)` → `WALLoaderService.replay()` | Records read back match originals (idempotent) |
| `test_wal_fallback_on_writer_failure` | `DataWriter.insert()` raises → recorder fallback | WAL file created in `tmp_path` |

**Integration tests:**

| Test | Setup | Assert |
|------|-------|--------|
| `test_recorder_service_drains_queue` | `RecorderService.run()` as async task; push 100 records to `recorder_queue` | `DataWriter.insert()` called; queue drained to 0 |
| `test_wal_first_mode_end_to_end` | `HFT_RECORDER_MODE=wal_first`; push records | WAL files created in `tmp_path`; replay loader reads them back with correct count |
| `test_recorder_drop_on_full_queue` | Fill `recorder_queue` to capacity; push 1 more via `put_nowait` | `QueueFull` handled gracefully; no crash; degraded mode metric set |

### 5.6 Plane 6: Observability & Safety (`test_06_observability_safety_plane.py`)

**Chain tests:**

| Test | Input | Assert |
|------|-------|--------|
| `test_storm_guard_fsm_transitions` | `NORMAL → feed_gap(31s) → HALT → recovery → NORMAL` | State transitions correct; `trigger_halt()` / `trigger_recovery()` work |
| `test_halt_blocks_risk_evaluation` | `StormGuard.state == HALT` + `RiskEngine.evaluate(intent)` | Intent rejected with halt reason |
| `test_halt_allows_cancel` | HALT state + CANCEL `OrderIntent` | Intent approved (cancel exempt from halt) |
| `test_metrics_counter_increment` | Call `metrics.inc("order_reject_total")` | Counter value incremented by 1 |

**Integration tests:**

| Test | Setup | Assert |
|------|-------|--------|
| `test_supervise_detects_service_crash` | Start supervisor loop; kill one service task | `storm_guard.trigger_halt()` called; restart attempted |
| `test_halt_drains_queues_preserves_cancel` | HALT triggered; `risk_queue` has 3 intents (1 CANCEL, 2 NEW) | After drain: CANCEL preserved, 2 NEW dropped |
| `test_feed_gap_triggers_halt` | No market data for `HFT_STORMGUARD_FEED_GAP_HALT_S` seconds | StormGuard transitions to HALT |
| `test_queue_depth_metrics_updated` | Push items to all queues; run 1 supervise cycle | Prometheus gauge values match actual queue depths |

### 5.7 Plane 7: Alpha Governance (`test_07_alpha_governance_plane.py`)

**Chain tests:**

| Test | Input | Assert |
|------|-------|--------|
| `test_gate_a_manifest_validation` | Valid alpha manifest | `run_gate_a()` passes |
| `test_gate_a_rejects_missing_fields` | Manifest missing required field | `run_gate_a()` fails with specific error |
| `test_gate_b_pytest_execution` | Alpha with passing tests | `run_gate_b()` passes |
| `test_gate_c_backtest_scorecard` | Alpha with backtest results | `run_gate_c()` produces scorecard with Sharpe/drawdown |
| `test_gate_d_threshold_evaluation` | Scorecard meeting thresholds | `_evaluate_gate_d()` approves |
| `test_gate_d_rejects_below_threshold` | Scorecard below Sharpe threshold | `_evaluate_gate_d()` rejects |
| `test_gate_e_shadow_session` | Mocked shadow session with good execution quality | `_evaluate_gate_e()` approves |

**Integration tests:**

| Test | Setup | Assert |
|------|-------|--------|
| `test_full_promotion_lifecycle` | Scaffold alpha → validate A-C → promote D-E (mocked scorecard/shadow) → canary evaluate → graduate | Config file written to `config/strategy_promotions/`; canary status is `graduated` |
| `test_promotion_rollback` | Promote → canary evaluate with bad metrics → rollback | Canary status is `rolled_back`; config removed or disabled |
| `test_gate_c_fail_blocks_promotion` | Alpha fails Gate C | Promotion CLI rejects; no config written |

## 6. Non-Goals

- Real broker connectivity (covered by `tests/manual/`)
- Performance/latency regression (covered by `tests/bench/`)
- Chaos/failure injection (covered by `tests/chaos/`)
- Load/stress testing (covered by `tests/stress/`)
- Pure-Python fallback paths (covered by existing unit tests with Rust disabled)

## 7. Test Count Summary

| Plane | Chain | Integration | Total |
|-------|-------|-------------|-------|
| 1. Control | 3 | 3 | 6 |
| 2. Market Data | 5 | 3 | 8 |
| 3. Decision | 4 | 3 | 7 |
| 4. Execution | 4 | 3 | 7 |
| 5. Persistence | 3 | 3 | 6 |
| 6. Observability & Safety | 4 | 4 | 8 |
| 7. Alpha Governance | 7 | 3 | 10 |
| **Total** | **30** | **22** | **52** |
