# services — Runtime Assembly & Supervision

> **Package**: `src/hft_platform/services/`
> **Runtime Plane**: Control
> **Files**: 11

## Overview

Application lifecycle management: builds 5 bounded queues + 18 services, supervision loop with lag/depth/liveness monitoring, and signal handling.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `system.py` | `HFTSystem` | Top-level runtime container |
| `bootstrap.py` | `SystemBootstrapper` | Service graph construction |
| `market_data.py` | `MarketDataService` (5 mixins) | Market data ingestion orchestrator |
| `registry.py` | `ServiceRegistry` | Named service lifecycle tracking |
| `heartbeat.py` | `HeartbeatService` | Task liveness and queue depth monitoring |
| + 6 more | — | Supporting services |

## HFTSystem

Top-level runtime container:

```python
system = HFTSystem(config)
await system.start()   # Bootstrap all services
await system.run()     # Main event loop
await system.stop()    # Graceful shutdown
```

- Owns 5 bounded queues: `raw_queue`, `raw_exec_queue`, `risk_queue`, `order_queue`, `recorder_queue`
- Owns 18+ services: MarketDataService, StrategyRunner, RiskEngine, OrderAdapter, RecorderService, etc.
- SIGTERM/SIGINT handler for graceful shutdown

## SystemBootstrapper

Builds the complete service graph:

1. Config loading (5-layer merge)
2. Broker client initialization
3. Queue creation (bounded)
4. Service instantiation in dependency order
5. StormGuard wiring
6. Supervisor task creation

## MarketDataService

Market data ingestion orchestrator with 5 mixins:

```
Broker callback (thread) → call_soon_threadsafe → raw_queue → MarketDataService.run()
  → normalize → LOB → FeatureEngine → RingBufferBus → record
```

- Processes raw market data from bounded `raw_queue`
- Normalizes, updates LOB, computes features
- Publishes to RingBufferBus for strategy consumption
- Records to recorder_queue (non-blocking, drops on full)

## ServiceRegistry

Named service lifecycle:
- Register, start, stop services by name
- Track running/stopped/failed states
- Expose health status for readiness checks

## HeartbeatService

Supervision loop:
- Task liveness checks (critical tasks must be alive)
- Queue depth monitoring (threshold alerts)
- Event loop lag measurement
- StormGuard trigger on sustained anomalies

## Configuration

Services configured via `config/base/main.yaml` + env overrides. See CLAUDE.md for full env var reference.
