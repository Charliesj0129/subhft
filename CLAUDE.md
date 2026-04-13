# CLAUDE.md — HFT Platform AI Context (The Constitution)

## 🏦 System Identity

- **Project**: `hft-platform` — 事件驅動高頻交易平台
- **Broker**: Shioaji (永豐金證券 API) / Fubon (富邦證券 API) — TWSE/OTC 股票 + 期貨; selected via `broker:` key in config or `HFT_BROKER` env var
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

- **Canonical architecture doc**: `docs/architecture/current-architecture.md` (354 lines, last updated 2026-04-12)
- **Feature/Lob/Research unification spec (TODO plan)**: `docs/architecture/feature-engine-lob-research-unification-spec.md`
- **Latency realism baseline (system vs Shioaji sim API RTT)**: `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`
- **C4 diagrams**: `.agent/library/c4-model-current.md`
- **Cluster evolution backlog**: `.agent/library/cluster-evolution-backlog.md`
- **Detailed governance rules**: `.agent/rules/` (auto-loaded)

### Runtime Pipeline

```
Exchange → BrokerFacade(Shioaji|Fubon) → Normalizer → LOBEngine → FeatureEngine → RingBufferBus → StrategyRunner → RiskEngine → OrderAdapter → BrokerFacade
                                                                          ↘ RecorderService → WAL / ClickHouse
```

> **Multi-broker**: The `BrokerFacade` is a polymorphic adapter selected at startup by `config.broker` / `HFT_BROKER`. Each implementation exposes identical `login()`, `subscribe()`, `place_order()`, `cancel_order()` interfaces. See `docs/architecture/multi-broker-support.md` for the ADR.

**Feature Engine** (Phase 18→v3): `FeatureEngine` sits between `LOBEngine` and `RingBufferBus`, computing 27 shared features across 3 schema versions (`lob_shared_v1`:16, `lob_shared_v2`:22, `lob_shared_v3`:27 — default). v1: 8 stateless + 8 rolling (OFI L1, EMA spread/imbalance). v2 adds: depth-normalized OFI, return autocovariance, TOB survival, impact surprise, deep depth momentum, trade-signed toxicity. v3 adds: multi-window EMA aggregation (5s/30s/300s OFI, imbalance, spread). Enabled by default (`HFT_FEATURE_ENGINE_ENABLED=1`); disable with `HFT_FEATURE_ENGINE_ENABLED=0`. Companion modules: `BurstDetector` (tick intensity surge detection). See `feature/engine.py`, `feature/registry.py`. Production hardening (Rust kernel promotion, parity testing) tracked in `docs/TODO.md`.

### Latency Realism Guard (Research / Backtest / Promotion)

**Do not assume local microsecond-stage latency implies executable trading latency.**

For Shioaji-driven strategies, the measured system internal lower-bound is on the order of **tens of microseconds**, but the measured **simulation API RTT** is on the order of **tens of milliseconds** (roughly **500x+** larger). See:

- `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`

Mandatory policy:
1. Model `place_order`, `update_order`, and `cancel_order` latencies separately in research/backtests.
2. Use at least **P95** latency assumptions for promotion decisions (P99 for stress tests).
3. Record latency assumptions in research artifacts; missing latency profile = non-promotion-ready.
4. Treat sub-broker-RTT alpha half-lives as optimistic until validated via shadow/live evidence.

### Runtime Planes (7)

| Plane         | Key Module                                                                           | Responsibility                              |
| ------------- | ------------------------------------------------------------------------------------ | ------------------------------------------- |
| Control       | `services/bootstrap.py`, `services/system.py`                                        | Bounded queues, service graph, supervision  |
| Market Data   | `feed_adapter/shioaji_client.py`, `normalizer.py`, `lob_engine.py`; fused path: `RustNormalizerLobFused` / `RustNormalizerFeatureFusedV1` | Ingest → normalize → LOB state (fused Rust path optional via `HFT_FUSED_NORMALIZER=1`) |
| Feature       | `feature/engine.py`, `feature/registry.py`, `feature/burst_detector.py`              | 27 LOB-derived features (v3), burst detection, research/live parity |
| Decision      | `strategy/runner.py`, `risk/engine.py`                                               | Strategy dispatch → risk validation         |
| Execution     | `order/adapter.py`, `execution/router.py`, `execution/positions.py`, `execution/execution_optimizer.py`, `execution/checkpoint.py`, `execution/startup_recon.py` | Broker API, fill routing, position tracking, limit/market decision, checkpoint, startup recon |
| Persistence   | `recorder/worker.py`, `recorder/batcher.py`, `recorder/writer.py`, `recorder/wal.py` | ClickHouse + WAL fallback                   |
| Observability | `observability/metrics.py`, `risk/storm_guard.py`                                    | Prometheus metrics, StormGuard FSM          |

### Non-Hot-Path Services (Cold Plane)

These modules run outside the tick-processing loop and are not latency-critical. Float arithmetic is permitted.

| Service        | Module                       | Purpose                                                                 |
| -------------- | ---------------------------- | ----------------------------------------------------------------------- |
| Notifications  | `notifications/`             | Telegram + webhook alert routing (23+ templates, critical retry)        |
| Telegram Bot   | `bot/`                       | Interactive commands (`/report`, `/levels`, `/flow`, `/status`, `/stop`) |
| TCA            | `tca/`                       | Transaction cost analysis (fee calc + 4-component slippage decomposition) |
| Reports        | `reports/`                   | Daily market analysis pipeline (collect → facts → reason → compose → distribute) |
| Monitor TUI    | `monitor/`                   | Live signal monitoring (ClickHouse + Redis + SHM hybrid data)           |
| Ops            | `ops/`                       | SessionGovernor, AutonomyMonitor, BackupManager, PositionFlattener      |
| Analytics      | `analytics/`                 | ClickHouse query templates for offline analysis                         |
| Diagnostics    | `diagnostics/`               | Event replay, decision trace sampling for post-mortem                   |
| Data Quality   | `data_quality/`              | Completeness checks, outlier detection, feed gap profiling              |

Module documentation: `docs/modules/<module>.md` for each service.

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

Research artifacts: `research/alphas/<alpha_id>/` (48+ implementations), experiment runs: `research/experiments/runs/`. Feature kernels unified via `FeatureEngine` (Phase 18); remaining parity work tracked in `docs/TODO.md`.

## 🦀 Rust Boundary (`rust_core` via PyO3)

Compiled extension at `src/hft_platform/rust_core.cpython-*.so`.

| Export                                                                                                                                                               | Purpose                                        |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `FastRingBuffer`, `EventBus`, `FastTickRingBuffer`, `FastBidAskRingBuffer`, `FastLOBStatsRingBuffer`                                                                 | Lock-free event routing / typed ring buffers   |
| `scale_book`, `scale_book_seq`, `scale_book_pair`, `scale_book_pair_stats`, `scale_book_pair_stats_np`, `compute_book_stats`, `get_field`                            | LOB scaling and book stats hot path            |
| `normalize_tick_tuple`, `normalize_bidask_tuple`, `normalize_bidask_tuple_np`, `normalize_bidask_tuple_with_synth`, `normalize_tick_v2`, `normalize_bidask_v2`       | Tick/BidAsk normalization (Python + v2 paths)  |
| `LimitOrderBook`                                                                                                                                                     | Full limit order book state                    |
| `RustBookState`                                                                                                                                                      | Lightweight LOB snapshot state                 |
| `RustPositionTracker`                                                                                                                                                | O(1) position accounting                       |
| `FastGate`, `RustRiskValidator`, `RustExposureStore`, `RustCircuitBreaker`, `RustStormGuardValidator`                                                                | Risk gate, validator, exposure tracking, breaker, storm guard |
| `RustDedupStore`                                                                                                                                                     | Idempotency / order deduplication              |
| `LobFeatureKernelV1`, `RustFeaturePipelineV1`, `RustFeatureEngineV2`                                                                                                | LOB feature kernels and feature engine         |
| `AlphaDepthSlope`, `AlphaOFI`, `AlphaRegimePressure`, `AlphaRegimeReversal`, `AlphaTransientReprice`, `AlphaMarkovTransition`, `MatchedFilterTradeFlow`, `MetaAlpha` | Alpha signal generators                        |
| `AlphaStrategy`                                                                                                                                                      | Rust-native strategy executor                  |
| `RustColumnarBuffer`                                                                                                                                                 | Columnar data buffer for batch recording       |
| `RustMetricsSampler`                                                                                                                                                 | Low-overhead Prometheus metrics sampler        |
| `to_ch_price_scaled`, `map_tick_record`, `map_bidask_record`, `map_order_record`, `map_fill_record`                                                                  | ClickHouse record mapping                      |
| `coerce_ns_int`, `coerce_ns_float`                                                                                                                                   | Timestamp coercion utilities                   |
| `ShmRingBuffer`, `ShmSnapshotTable`                                                                                                                                  | Shared memory IPC and snapshot table           |
| `SymbolInternTable` *(Wave 4)*                                                                                                                                       | Symbol string interning (O(1) lookup)          |
| `FastTypedRingBuffer` *(Wave 4)*                                                                                                                                     | Typed, cache-friendly ring buffer              |
| `RustGatewayFusedCheck` *(Wave 4)*                                                                                                                                   | Fused gateway risk check (zero-copy)           |
| `RustNormalizerLobFused` *(Wave 4)*                                                                                                                                  | Fused normalizer + LOB pipeline                |
| `RustNormalizerFeatureFusedV1` *(Wave 4)*                                                                                                                            | Fused normalizer + LOB + feature pipeline      |

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
| `HFT_MODE`                 | `sim`       | Runtime mode: `sim` / `real` / `replay`   |
| `HFT_ORDER_MODE`           | `sim`       | Order execution: `sim` / `live` (LIVE = real money) |
| `HFT_SYMBOLS`              | —           | Comma-separated symbol list override      |
| `HFT_QUOTE_VERSION`        | `auto`      | Shioaji quote protocol version            |
| `HFT_STRICT_PRICE_MODE`    | `0`         | `1` = reject float prices with TypeError  |
| `HFT_GATEWAY_ENABLED`      | `0`         | `1` = enable CE-M2 order/risk gateway     |
| `HFT_RECORDER_MODE`        | `direct`    | `wal_first` = WAL-only write path (CE-M3) |
| `HFT_CLICKHOUSE_HOST`      | `localhost` | ClickHouse host                           |
| `HFT_EXPOSURE_MAX_SYMBOLS` | `10000`     | ExposureStore cardinality bound           |
| `HFT_BROKER`               | `shioaji`   | Broker backend: `shioaji` / `fubon`       |
| `HFT_FEATURE_ENGINE_ENABLED` | `1`         | `0` = disable FeatureEngine in runtime pipeline (default: v3 with 27 features) |
| `HFT_FUSED_NORMALIZER`     | `0`         | `1` = enable fused Rust normalizer+LOB pipeline |
| `HFT_FEATURE_ENGINE_BACKEND` | `python`  | Backend for FeatureEngine: `python` / `rust`    |
| `HFT_FUBON_CERT_PATH`      | —           | Fubon API certificate file path           |
| `HFT_FUBON_ACCOUNT`        | —           | Fubon trading account ID                  |
| `HFT_FUBON_PASSWORD`       | —           | Fubon account password (use secret mgr)   |
| `HFT_MONITOR_SOURCE`       | `clickhouse`| Monitor data source: `clickhouse`/`redis`/`hybrid` |
| `HFT_MONITOR_LIVE_ENABLED` | `0`         | `1` = enable Redis live publisher in MarketDataService |
| `HFT_MONITOR_REDIS_HOST`   | `localhost` | Redis host for monitor live cache         |
| `HFT_MONITOR_REDIS_PORT`   | `6379`      | Redis port for monitor live cache         |
| `HFT_MONITOR_REDIS_PASSWORD`| —          | Redis password for monitor live cache     |
| `HFT_MONITOR_DATA_SOURCE`  | `auto`      | Data source layer: `ch`/`shm`/`auto`     |
| `HFT_RECONNECT_HOURS`     | `08:30-13:35`| Trading hours window for auto-reconnect  |
| `HFT_RECONNECT_HOURS_2`   | —           | Secondary trading hours window            |
| `HFT_RECONNECT_COOLDOWN`  | `60`        | Reconnect cooldown seconds                |
| `HFT_RECONNECT_BACKOFF_S` | `5`         | Initial reconnect backoff delay seconds   |
| `HFT_RECONNECT_BACKOFF_MAX_S`| `120`    | Maximum reconnect backoff delay seconds   |
| `HFT_QUOTE_FLAP_THRESHOLD`| `5`         | Quote flap detection: max flaps in window |
| `HFT_QUOTE_FLAP_WINDOW_S` | `60`        | Quote flap detection window seconds       |
| `HFT_QUOTE_FLAP_COOLDOWN_S`| `300`      | Quote flap cooldown before re-subscribe   |
| `HFT_STORMGUARD_FEED_GAP_STORM_S`| `1.0` | Feed gap threshold (seconds) to trigger STORM. Feed gap alone cannot trigger HALT. |
| `HFT_STORMGUARD_FEED_GAP_HALT_S`| `30`  | **Deprecated** alias for `_STORM_S`. Maps to STORM (not HALT). |
| `HFT_BACKUP_ENABLED`        | `0`                    | `1` = enable automated daily ClickHouse backup |
| `HFT_BACKUP_RETAIN_DAYS`    | `30`                   | Number of daily backups to retain               |
| `CH_BACKUP_PATH`            | `./backups/clickhouse`  | Host path for ClickHouse backup volume mount    |
| `HFT_STARTUP_RECON_ENABLED`              | `1`   | Enable startup position recovery            |
| `HFT_STARTUP_RECON_QTY_THRESHOLD`        | `10`  | Stock discrepancy auto-correct threshold    |
| `HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD`| `2`   | Futures discrepancy auto-correct threshold  |
| `HFT_CHECKPOINT_ENABLED`                 | `1`   | Enable periodic position checkpoint writing |
| `HFT_ORDER_SHADOW_MODE`  | `0`         | `1` = shadow order interception (orders never reach broker) |
| `HFT_RECONNECT_DAYS`    | `mon,tue,wed,thu,fri` | Weekdays for auto-reconnect              |
| `HFT_RECONNECT_TZ`      | `Asia/Taipei`| Timezone for reconnect hours             |
| `HFT_ARCHIVE_RETENTION_DAYS` | `3`    | WAL archive retention days                |
| `HFT_TELEGRAM_ENABLED`  | `0`         | `1` = enable Telegram notification bot    |
| `HFT_TELEGRAM_BOT_TOKEN`| —           | Telegram bot token (use secret mgr)       |
| `HFT_TELEGRAM_CHAT_ID`  | —           | Telegram chat ID for alerts               |

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
