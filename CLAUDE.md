# CLAUDE.md — HFT Platform AI Context (The Constitution)

## 🏦 System Identity

- **Project**: `hft-platform` — 事件驅動高頻交易平台
- **Broker**: Shioaji (永豐金證券 API) — TWSE/OTC 股票 + 期貨
- **Stack**: Python 3.12 + Rust (PyO3 via `rust_core`) + ClickHouse + Prometheus
- **Entry CLI**: `hft run sim|live` → `src/hft_platform/cli.py`

## 🛡️ Critical HFT Laws (THE CONSTITUTION)

**Violation of these laws causes critical latency penalties or financial loss.**

1.  **Allocator Law**:
    - ❌ **BAD**: `data = [x for x in range(1000)]` (in tick loop)
    - ✅ **GOOD**: `self.buffer[i] = x` (Pre-allocated)
    - **Rule**: No `malloc` or heap allocations on the Hot Path. Use Object Pooling.
2.  **Cache Law**:
    - ❌ **BAD**: Array of Objects (Pointer Chasing)
    - ✅ **GOOD**: Structure of Arrays (Contiguous Memory)
    - **Rule**: Data must be packed for locality. Use `numpy` or Rust `Vec`.
3.  **Async Law**:
    - ❌ **BAD**: `requests.get()`, `time.sleep()`, `json.loads(big_file)`
    - ✅ **GOOD**: `await client.get()`, `await asyncio.sleep()`, `orjson` in thread pool
    - **Rule**: No blocking IO or compute > 1ms on the main loop.
4.  **Precision Law**:
    - ❌ **BAD**: `price = 100.1 + 0.2` (Float)
    - ✅ **GOOD**: `price = Decimal('100.1')` or `price_micros = 100100000`
    - **Rule**: Never use `float` for prices/balances. All prices are **scaled int (x10000)**.
5.  **Boundary Law**:
    - ❌ **BAD**: Copying large lists between Python and Rust.
    - ✅ **GOOD**: `PyBuffer` protocol, Arrow, Shared Memory.
    - **Rule**: Zero-Copy Interfaces only.

## 🏛️ Architecture Quick Reference

- **Canonical architecture doc**: `docs/architecture/current-architecture.md` (252 lines, continuously updated)
- **Feature/Lob/Research unification spec (TODO plan)**: `docs/architecture/feature-engine-lob-research-unification-spec.md`
- **Latency realism baseline (system vs Shioaji sim API RTT)**: `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`
- **C4 diagrams**: `.agent/library/c4-model-current.md`
- **Cluster evolution backlog**: `.agent/library/cluster-evolution-backlog.md`
- **Detailed governance rules**: `.agent/rules/` (auto-loaded)

### Runtime Pipeline

```
Exchange → ShioajiClient → Normalizer → LOBEngine → RingBufferBus → StrategyRunner → RiskEngine → OrderAdapter → Broker
                                                            ↘ RecorderService → WAL / ClickHouse
```

Planned (TODO, not implemented yet): insert a **Feature Plane / FeatureEngine** between `LOBEngine` and `StrategyRunner` for shared microstructure feature computation and research/live parity. See `docs/architecture/feature-engine-lob-research-unification-spec.md`.

### Latency Realism Guard (Research / Backtest / Promotion)

**Do not assume local microsecond-stage latency implies executable trading latency.**

For Shioaji-driven strategies, the measured system internal lower-bound is on the order of **tens of microseconds**, but the measured **simulation API RTT** is on the order of **tens of milliseconds** (roughly **500x+** larger). See:

- `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`

Mandatory policy:
1. Model `place_order`, `update_order`, and `cancel_order` latencies separately in research/backtests.
2. Use at least **P95** latency assumptions for promotion decisions (P99 for stress tests).
3. Record latency assumptions in research artifacts; missing latency profile = non-promotion-ready.
4. Treat sub-broker-RTT alpha half-lives as optimistic until validated via shadow/live evidence.

### Runtime Planes (6)

| Plane         | Key Module                                                                           | Responsibility                              |
| ------------- | ------------------------------------------------------------------------------------ | ------------------------------------------- |
| Control       | `services/bootstrap.py`, `services/system.py`                                        | Bounded queues, service graph, supervision  |
| Market Data   | `feed_adapter/shioaji_client.py`, `normalizer.py`, `lob_engine.py`                   | Ingest → normalize → LOB state              |
| Decision      | `strategy/runner.py`, `risk/engine.py`                                               | Strategy dispatch → risk validation         |
| Execution     | `order/adapter.py`, `execution/router.py`, `execution/positions.py`                  | Broker API, fill routing, position tracking |
| Persistence   | `recorder/worker.py`, `recorder/batcher.py`, `recorder/writer.py`, `recorder/wal.py` | ClickHouse + WAL fallback                   |
| Observability | `observability/metrics.py`, `risk/storm_guard.py`                                    | Prometheus metrics, StormGuard FSM          |

### Planned Feature Plane (TODO, Not Yet Implemented)

| Plane | Candidate Modules | Responsibility |
| ----- | ----------------- | -------------- |
| Feature (planned) | `feature/engine.py`, `feature/registry.py` (TBD) | Shared LOB-derived feature kernels for research/backtest/live parity |

## 📦 Key Data Contracts (Scaled Int Convention)

All price fields are `int` scaled by **x10000** (configurable per symbol in `symbols.yaml`).

```
OrderIntent → (Risk) → RiskDecision → OrderCommand → (Broker) → FillEvent → PositionDelta
```

| Contract        | File                     | Key Fields                                                          |
| --------------- | ------------------------ | ------------------------------------------------------------------- |
| `OrderIntent`   | `contracts/strategy.py`  | `price: int`, `qty: int`, `side: Side`, `idempotency_key`, `ttl_ns` |
| `OrderCommand`  | `contracts/strategy.py`  | `cmd_id`, `deadline_ns`, `storm_guard_state`                        |
| `FillEvent`     | `contracts/execution.py` | `price: int`, `fee: int`, `tax: int` (all x10000)                   |
| `PositionDelta` | `contracts/execution.py` | `net_qty`, `avg_price: int`, `realized_pnl: int`                    |
| `TickEvent`     | `events.py`              | `price: int` (x10000), `volume`, `meta: MetaData`                   |
| `BidAskEvent`   | `events.py`              | `bids/asks: np.ndarray` shape (N,2), `stats: tuple`                 |
| `LOBStatsEvent` | `events.py`              | `mid_price_x2: int`, `spread_scaled: int`, `imbalance: float`       |

## ⚙️ Config Priority Chain

Settings are resolved via layered merge in `config/loader.py`:

```
Base YAML (config/base/main.yaml)
  → Env YAML (config/env/{mode}/main.yaml)
    → settings.py (config/settings.py)
      → Environment Variables (HFT_MODE, HFT_SYMBOLS, ...)
        → CLI Overrides (--mode, --symbols, ...)
```

## 🧬 Alpha Governance Pipeline

Full lifecycle from research to production, implemented in `src/hft_platform/alpha/`:

| Gate       | Check                              | Module                                 |
| ---------- | ---------------------------------- | -------------------------------------- |
| **A**      | Manifest + data-field + complexity | `alpha/validation.py::run_gate_a`      |
| **B**      | Per-alpha pytest execution         | `alpha/validation.py::run_gate_b`      |
| **C**      | Standardized backtest + scorecard  | `alpha/validation.py::run_gate_c`      |
| **D**      | Sharpe/drawdown thresholds         | `alpha/promotion.py::_evaluate_gate_d` |
| **E**      | Shadow session + execution quality | `alpha/promotion.py::_evaluate_gate_e` |
| **Canary** | Hold/escalate/rollback/graduate    | `alpha/canary.py`                      |

Research artifacts: `research/alphas/<alpha_id>/`, experiment runs: `research/experiments/runs/`.
Planned (TODO): unify shared LOB-derived feature kernels across research replay, `hftbacktest`, and live runtime via the FeatureEngine spec in `docs/architecture/feature-engine-lob-research-unification-spec.md`.

## 🦀 Rust Boundary (`rust_core` via PyO3)

Compiled extension at `src/hft_platform/rust_core.cpython-*.so`.

| Export                                                                                                                                                               | Purpose                       |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| `FastRingBuffer`, `EventBus`, `FastTickRingBuffer`                                                                                                                   | Lock-free event routing       |
| `scale_book*`, `normalize_*`, `compute_book_stats`, `get_field`                                                                                                      | LOB normalization hot path    |
| `RustPositionTracker`                                                                                                                                                | O(1) position accounting      |
| `FastGate`                                                                                                                                                           | Numba-equivalent risk gate    |
| `AlphaDepthSlope`, `AlphaOFI`, `AlphaRegimePressure`, `AlphaRegimeReversal`, `AlphaTransientReprice`, `AlphaMarkovTransition`, `MatchedFilterTradeFlow`, `MetaAlpha` | Alpha signal generators       |
| `AlphaStrategy`                                                                                                                                                      | Rust-native strategy executor |
| `ShmRingBuffer`                                                                                                                                                      | Shared memory IPC             |

## 🤖 Commands

- **Setup**: `sudo ./ops.sh setup` (Installs Docker, creates dirs)
- **Install**: `uv sync` or `pip install -e .`
- **Build Rust**: `uv run maturin develop --manifest-path rust_core/Cargo.toml`
- **Test**: `make test` (unit) / `make test-all` (unit+integration)
- **Lint**: `make lint` → `ruff check src/ tests/`
- **Type Check**: `make typecheck` → `mypy`
- **CI locally**: `make ci` (format-check + lint + typecheck + coverage)
- **Run**: `uv run hft run sim` or `python -m hft_platform run sim`
- **Docker**: `make start` / `make stop` / `make logs`

## 🌐 Critical Environment Variables

| Variable                   | Default     | Purpose                                   |
| -------------------------- | ----------- | ----------------------------------------- |
| `HFT_MODE`                 | `sim`       | Runtime mode: `sim` / `live` / `replay`   |
| `HFT_SYMBOLS`              | —           | Comma-separated symbol list override      |
| `HFT_QUOTE_VERSION`        | `auto`      | Shioaji quote protocol version            |
| `HFT_STRICT_PRICE_MODE`    | `0`         | `1` = reject float prices with TypeError  |
| `HFT_GATEWAY_ENABLED`      | `0`         | `1` = enable CE-M2 order/risk gateway     |
| `HFT_RECORDER_MODE`        | `direct`    | `wal_first` = WAL-only write path (CE-M3) |
| `HFT_CLICKHOUSE_HOST`      | `localhost` | ClickHouse host                           |
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000`     | ExposureStore cardinality bound           |

## 🎨 Coding Style (Strict)

- **Python**: Type hints (3.12+), `structlog` (no print), `pydantic`/`msgspec` for schemas, `__slots__` on dataclasses.
- **Rust**: `clippy` strict, `pyo3` bindings, `thiserror` for errors.
- **Commits**: Conventional Commits (`feat:`, `fix:`, `perf:`).
- **Timestamps**: Always `timebase.now_ns()` — never `datetime.now()` or `time.time()`.

## 🚩 Red Flags (Code Review)

- [ ] Any `float` usage in financial logic? → **REJECT**
- [ ] `import pandas` in hot path? → **REJECT** (Too slow)
- [ ] `unwrap()` in Rust code reachable from Python? → **REJECT** (Panic risk)
- [ ] No unit tests for new logic? → **REJECT**
- [ ] `datetime.now()` instead of `timebase.now_ns()`? → **REJECT**
- [ ] Missing `__slots__` on new hot-path dataclass? → **WARN**
- [ ] `print()` instead of `structlog`? → **REJECT**
- [ ] Research/backtest assumes zero-latency or mean-only broker RTT for Shioaji? → **REJECT**
