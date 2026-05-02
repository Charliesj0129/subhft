# HFT Platform Architecture

This document is the **entry point** for architecture documentation. The canonical, detailed architecture baseline is maintained separately and continuously updated.

---

## System Overview (Pipeline)

```
Market Data → Normalizer → LOB → FeatureEngine → EventBus → Strategy → Risk → Order → Broker
                                                       ↘ Recorder → WAL / ClickHouse
```

> **Note:** FeatureEngine is enabled by default (`HFT_FEATURE_ENGINE_ENABLED=1`). Set to `0` to disable, in which case LOB events flow directly to EventBus.

Key goals:

- Low latency (hot paths in Rust/Numba)
- Deterministic timing (minimize blocking I/O)
- Observability first (Prometheus metrics everywhere)

---

## Canonical Architecture Document

> **The single source of truth for detailed architecture is:**
> [`docs/architecture/current-architecture.md`](architecture/current-architecture.md)
>
> It covers: Runtime Planes (7), Module Inventory, Rust Boundary, Persistence Surfaces, Architectural Invariants, Observed Drift, and Cluster Evolution status (CE-M2/M3).

Companion documents:

- [Target Architecture](architecture/target-architecture.md)

---

## Runtime Service Topology (Docker Compose)

| Service        | Image / Entrypoint                  | Port           | Purpose                  |
| -------------- | ----------------------------------- | -------------- | ------------------------ |
| `hft-engine`   | `python -m hft_platform.main`       | 9090 (metrics) | Core trading runtime     |
| `clickhouse`   | `clickhouse-server:25.12.3`         | 8123, 9000     | Time-series storage      |
| `redis`        | `redis:7`                           | 6379           | Optional cache / pub-sub |
| `wal-loader`   | `hft_platform.recorder.loader`      | —              | WAL → ClickHouse replay  |
| `hft-monitor`  | `scripts/monitor_runtime_health.py` | —              | Runtime health checks    |
| `prometheus`   | `prom/prometheus:v2.51.0`           | 9091           | Metrics collection       |
| `alertmanager` | `prom/alertmanager:v0.27.0`         | 9093           | Alert routing            |
| `grafana`      | `grafana/grafana:10.4.1`            | 3000           | Dashboards               |

---

## Rust Acceleration (`rust_core` via PyO3)

The `rust_core` extension provides hot-path acceleration consumed by:

- `feed_adapter/normalizer.py` — tick/bidask normalization
- `feed_adapter/lob_engine.py` — book stats computation
- `engine/event_bus.py` — ring buffer
- `execution/positions.py` — position tracking
- `strategies/rust_alpha.py` — alpha signal generation

Build: `uv run maturin develop --manifest-path rust_core/Cargo.toml`

---

## Storage

- **WAL**: jsonl files under `.wal/` (WAL-first mode: `HFT_RECORDER_MODE=wal_first`)
- **ClickHouse** (15 migrations, 13+ tables):
  - Core: `hft.market_data` (6mo TTL), `hft.orders`, `hft.trades`/`hft.fills` (1yr TTL)
  - Derived: `hft.ohlcv_1m` (MV), `hft.latency_stats_1m` (MV)
  - Operations: `hft.pnl_snapshots` (90d), `hft.shadow_orders` (30d), `hft.reconciliation` (1yr)
  - TCA: `hft.slippage_records` (90d), decision_price/arrival_price in fills
  - Audit: `hft.config_snapshots` (1yr), `hft.daily_reports`, `hft.liquidity_gate_events`
  - Replay: `hft.wal_dedup` (WAL replay deduplication)
  - Audit namespace: `audit.*` (2yr TTL, compliance)
- **Migrations**: `src/hft_platform/migrations/clickhouse/` (auto-applied on boot)

---

## Related Docs

- [docs/README.md](../README.md) — documentation index
- [Getting Started](../guides/getting-started.md) — full usage guide
- [Config Reference](../guides/config-reference.md) — configuration reference
- [Observability](../operations/observability.md) — metrics & alerts
- [Modules Reference](../MODULES_REFERENCE.md) — consolidated codebase map
- [Env Vars Reference](../operations/env-vars-reference.md) — 60+ environment variables
- `CLAUDE.md` — AI context & HFT Laws
