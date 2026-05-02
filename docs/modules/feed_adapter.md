# feed_adapter — Multi-Broker Market Data Ingestion

> **Package**: `src/hft_platform/feed_adapter/`
> **Runtime Plane**: Market Data
> **Hot-Path**: `Normalizer.normalize_tick()`, `LOBEngine.process_event()`

## Overview

Multi-broker ingestion via registry pattern. Normalizes raw broker data to `TickEvent`/`BidAskEvent`, maintains per-symbol LOB state, emits `LOBStatsEvent`. Extensive Rust fast-paths. 20+ files across Shioaji and Fubon sub-packages.

## Architecture

```
Exchange → BrokerFacade (Shioaji|Fubon)
  → Normalizer [Rust fast-path]
  → LOBEngine [Rust fast-path]
  → RingBufferBus
```

## Sub-Packages

| Package | Files | Key Classes | Purpose |
|---------|-------|-------------|---------|
| Top-level | 5+ | `Normalizer`, `LOBEngine`, `broker_registry`, `protocols` | Shared normalization, LOB, broker abstractions |
| `_base/` | 3+ | `BaseBrokerSessionRuntime`, `BaseQuoteWatchdog`, `CooldownManager` | Shared broker abstractions |
| `shioaji/` | 20 | `ShioajiClientFacade`, `ShioajiClient`, `TickDispatcher`, `ReconnectOrchestrator` | Full Shioaji integration |
| `fubon/` | 14 | `FubonClientFacade`, `FubonQuoteRuntime` | Full Fubon integration |

## Broker Registry Pattern

```python
from hft_platform.feed_adapter.broker_registry import get_broker_factory

factory = get_broker_factory("shioaji")  # or "fubon"
client = factory(config)
```

- Selection: `HFT_BROKER` env var (default: `"shioaji"`)
- Auto-registration: each broker's `__init__.py` registers on import
- Import guarding: `try: import shioaji except ImportError: shioaji = None`

## Protocols (MB-01)

All brokers must implement 4 `@runtime_checkable` protocols:

| Protocol | Key Methods |
|----------|-------------|
| `MarketDataProvider` | `subscribe()`, `unsubscribe()` |
| `OrderExecutor` | `place_order()`, `cancel_order()`, `update_order()` |
| `AccountProvider` | `get_positions()`, `get_margin()` |
| `BrokerSession` | `login()`, `logout()`, `is_connected()` |

## Normalizer

Converts raw broker tick/bidask data to platform events:

```python
tick_event = normalizer.normalize_tick(raw_data)     # → TickEvent
bidask_event = normalizer.normalize_bidask(raw_data)  # → BidAskEvent
```

- All prices scaled to x10000 at ingestion boundary (MB-07)
- Rust fast-path: `normalize_tick_tuple()`, `normalize_bidask_tuple_np()`
- Fused path: `RustNormalizerLobFused` / `RustNormalizerFeatureFusedV1` via `HFT_FUSED_NORMALIZER=1`

## LOBEngine

Maintains per-symbol limit order book state:

```python
stats_event = lob_engine.process_event(bidask_event)  # → LOBStatsEvent
l1 = lob_engine.get_l1_scaled(symbol)  # Fast L1 snapshot
```

- Computes: mid_price_x2, spread_scaled, imbalance, bid/ask depth
- Rust fast-path: `scale_book_pair_stats_np()`, `compute_book_stats()`

## Shioaji Sub-Package (20 files)

| Component | File | Purpose |
|-----------|------|---------|
| `ShioajiClientFacade` | `facade.py` | Unified interface composing sub-runtimes |
| `SessionRuntime` | `session_runtime.py` | Login, reconnect, session lifecycle |
| `QuoteRuntime` | `quote_runtime.py` | Market data subscription and callbacks |
| `OrderGateway` | `order_gateway.py` | Order placement, cancellation |
| `AccountGateway` | `account_gateway.py` | Position, margin queries |
| `ContractsRuntime` | `contracts_runtime.py` | Contract and symbol resolution |
| `TickDispatcher` | `tick_dispatcher.py` | Async worker thread for tick dispatch |
| `ReconnectOrchestrator` | `reconnect_orchestrator.py` | Auto-reconnect with backoff |
| `QuoteConnectionPool` | `quote_connection_pool.py` | 5 conn x 200 = 1000 symbol slots |

## Fubon Sub-Package (14 files)

Same structure as Shioaji. Pre-allocated translation buffers, 10s cooldown.

## Quote Connection Pool

- 5 Shioaji API connections, each with 200 symbol capacity
- Total: 1000 symbol subscription slots
- Round-robin assignment with rebalancing

## Reconnect Strategy

| Parameter | Env Var | Default |
|-----------|---------|---------|
| Trading hours | `HFT_RECONNECT_HOURS` | `08:30-13:35` |
| Secondary hours | `HFT_RECONNECT_HOURS_2` | — |
| Cooldown | `HFT_RECONNECT_COOLDOWN` | `60s` |
| Initial backoff | `HFT_RECONNECT_BACKOFF_S` | `5s` |
| Max backoff | `HFT_RECONNECT_BACKOFF_MAX_S` | `120s` |

### Quote Flap Detection

- Threshold: `HFT_QUOTE_FLAP_THRESHOLD` (5 flaps in window)
- Window: `HFT_QUOTE_FLAP_WINDOW_S` (60s)
- Cooldown: `HFT_QUOTE_FLAP_COOLDOWN_S` (300s)

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_BROKER` | `shioaji` | Active broker backend |
| `HFT_QUOTE_VERSION` | `auto` | Shioaji quote protocol version |
| `HFT_FUSED_NORMALIZER` | `0` | Enable fused Rust normalizer+LOB |
| `HFT_STRICT_PRICE_MODE` | `0` | Reject float prices with TypeError |
| `SHIOAJI_API_KEY` | — | Shioaji broker API key |
| `SHIOAJI_SECRET_KEY` | — | Shioaji broker secret key |

## Governance Rules (from `.agent/rules/26-multi-broker-governance.md`)

- **MB-02**: No broker-specific imports outside `feed_adapter/<broker>/`
- **MB-04**: ExecutionNormalizer uses BrokerExecFieldMap (no hardcoded fields)
- **MB-07**: All prices scaled to x10000 at ingestion boundary
- **MB-08**: Distinct credential env vars per broker
- **MB-10**: Import failure → refuse to start (no silent fallback)
