# HFT Platform Architecture

This document is the **entry point** for architecture documentation. The canonical, detailed architecture baseline is maintained separately and continuously updated.

---

## System Overview (Pipeline)

```
Market Data → Normalizer → LOB → EventBus → Strategy → Risk → Order → Broker
                                      ↘ Recorder → WAL / ClickHouse
```

Key goals:

- Low latency (hot paths in Rust/Numba)
- Deterministic timing (minimize blocking I/O)
- Observability first (Prometheus metrics everywhere)

---

## Canonical Architecture Document

> **The single source of truth for detailed architecture is:**
> [`docs/architecture/current-architecture.md`](architecture/current-architecture.md)
>
> It covers: Runtime Planes (6), Module Inventory, Rust Boundary, Persistence Surfaces, Architectural Invariants, Observed Drift, and Cluster Evolution status (CE-M2/M3).

Companion documents:

- [C4 Model Diagrams](architecture/c4-model-current.md)
- [Cluster Evolution Backlog](architecture/cluster-evolution-backlog.md)
- [Design Review Artifacts](architecture/design-review-artifacts.md)
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

- **WAL**: jsonl files under `.wal/`
- **ClickHouse**: `hft.market_data`, `hft.orders`, `hft.trades`, `hft.ohlcv_1m`, `hft.latency_stats_1m`, `hft.latency_spans`
- **Schema**: `src/hft_platform/schemas/clickhouse.sql`

---

## Related Docs

- `docs/README.md` — documentation index
- `docs/getting_started.md` — full usage guide
- `docs/config_reference.md` — configuration reference
- `docs/observability_minimal.md` — metrics & alerts
- `CLAUDE.md` — AI context & HFT Laws
