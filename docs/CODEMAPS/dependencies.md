<!-- Generated: 2026-03-30 | Files scanned: 312 | Token estimate: ~650 -->

# Dependencies Codemap

## Infrastructure Services (docker-compose.yml)

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| hft-engine | hft-platform:latest | 9090 | Main trading runtime (Prometheus /metrics) |
| clickhouse | clickhouse-server:25.12.3 | 8123, 9000 | Primary persistence (market_data, orders, trades, fills) |
| redis | redis:7.4 | 6379 | Session lease, live monitor cache |
| wal-loader | hft-platform:latest | - | WAL replay to ClickHouse |
| prometheus | prom/prometheus:v2.51.0 | 9091 | Metrics scraper |
| alertmanager | prom/alertmanager:v0.27.0 | 9093 | Alert routing |
| grafana | grafana/grafana:* | 3000 | Dashboards |
| loki | grafana/loki:3.0.0 | 3100 | Log aggregation |
| promtail | grafana/promtail:3.0.0 | - | Log shipper |
| node-exporter | prom/node-exporter:v1.8.2 | - | Host metrics |

## Runtime Python Dependencies

### Core
| Package | Version | Purpose |
|---------|---------|---------|
| numpy | >=2.2 | Arrays, LOB state, contiguous buffers |
| pandas | >=2.3 | Analytics, reporting (NOT hot path) |
| clickhouse-connect | >=0.10 | ClickHouse client |
| structlog | >=25.1 | Structured logging |
| msgspec | >=0.20 | Fast serialization |
| orjson | >=3.10 | JSON (hot path safe) |
| PyYAML | >=6.0 | Config parsing |

### Performance
| Package | Version | Purpose |
|---------|---------|---------|
| uvloop | >=0.22 | Fast async event loop |
| numba | >=0.61 | JIT kernels (IPC, ShmRingBuffer) |
| maturin | (dev) | Rust extension build |

### Trading
| Package | Version | Purpose |
|---------|---------|---------|
| shioaji[speed] | ==1.2.9 (pinned) | SinoPac broker SDK (optional) |
| fubon-neo | (private) | Fubon broker SDK (optional) |
| hftbacktest | >=2.4 | Backtest simulator |
| exchange-calendars | >=4.13.1 | Trading hours |

### Observability
| Package | Version | Purpose |
|---------|---------|---------|
| prometheus_client | >=0.23 | Metrics export |
| psutil | >=7.1 | System resource monitoring |

### Research (optional group)
| Package | Version | Purpose |
|---------|---------|---------|
| optuna | >=4.7.0 | Hyperparameter tuning |
| scipy | >=1.15.3 | Statistics |
| torch | * | ML models |
| scikit-learn | * | Feature analysis |
| statsmodels | * | Time series |

### Application (optional extras)
| Package | Version | Purpose |
|---------|---------|---------|
| rich | >=13.0 | Signal monitor TUI |
| python-telegram-bot | >=21.0 | Telegram bot service |

## Rust Extension (rust_core)

```
Built via: maturin develop --manifest-path rust_core/Cargo.toml
Loaded as: hft_platform.rust_core (or fallback: rust_core)
Exports: 48+ PyO3 classes/functions (see data.md)
Source: 47 .rs files in rust_core/src/
```

## Broker SDK Import Guards

```python
# All broker SDKs use try/except ImportError guard
# Platform starts without all broker SDKs installed
# HFT_BROKER selects active broker at runtime
```

## External APIs

| API | Module | Purpose |
|-----|--------|---------|
| Shioaji | feed_adapter/shioaji/ | TWSE/OTC market data + orders |
| Fubon | feed_adapter/fubon/ | TWSE/OTC market data + orders |
| Telegram | notifications/telegram.py, bot/ | Alert notifications + interactive bot |
| arXiv | research/tools/ | Paper fetching (MCP) |
