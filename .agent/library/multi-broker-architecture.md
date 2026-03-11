# Multi-Broker Architecture Specification

Date: 2026-03-11
Status: Design specification (not yet implemented)
Companion docs: `.agent/library/current-architecture.md`, `.agent/library/target-architecture.md`, `.agent/library/shioaji-client-resilience-decoupling-plan.md`.
Prerequisite: D2 (Shioaji adapter decomposition) should be completed first.

## 1. Overview

### Current State

The platform is tightly coupled to a single broker (Shioaji / 永豐金證券). All broker concerns — session management, market data subscription, order placement, account queries, and contract resolution — are centralized in `src/hft_platform/feed_adapter/shioaji_client.py`. The decomposition plan (D2) has begun extracting `SessionRuntime` and `QuoteRuntime`, but the adapter still assumes a single broker.

### Target State

A pluggable multi-broker architecture where:

1. A `BrokerProtocol` abstraction defines the contract between Platform Core and any broker adapter.
2. A `BrokerFactory` registry creates the correct broker instance at startup.
3. Broker selection is driven by `HFT_BROKER` environment variable (default: `shioaji`).
4. Each broker lives in its own submodule under `feed_adapter/<broker>/`.
5. Platform Core (Strategy, Risk, Gateway, Recorder) remains broker-agnostic.

```
HFT_BROKER=shioaji|fubon
```

## 2. BrokerProtocol Boundary

```
                    ┌─────────────────────┐
                    │   Platform Core     │
                    │ (Strategy, Risk,    │
                    │  Gateway, Recorder) │
                    └────────┬────────────┘
                             │ BrokerProtocol
                    ┌────────┴────────────┐
              ┌─────┤   BrokerFactory     ├─────┐
              │     └─────────────────────┘     │
    ┌─────────┴──────────┐        ┌─────────────┴──────────┐
    │  ShioajiClient     │        │  FubonClient           │
    │  (feed_adapter/    │        │  (feed_adapter/        │
    │   shioaji/)        │        │   fubon/)              │
    └────────────────────┘        └────────────────────────┘
```

`BrokerProtocol` is defined using `typing.Protocol` (structural subtyping). This means the existing `ShioajiClientFacade` can satisfy the protocol without inheriting from it, enabling incremental migration.

Required protocol methods (indicative):

- `login() -> None` — authenticate and establish session
- `subscribe(symbols: list[str]) -> None` — start market data feed
- `unsubscribe(symbols: list[str]) -> None` — stop market data feed
- `place_order(cmd: OrderCommand) -> str` — submit order, return broker order ID
- `cancel_order(broker_order_id: str) -> None` — cancel an open order
- `amend_order(broker_order_id: str, cmd: OrderCommand) -> None` — modify an open order
- `get_positions() -> list[PositionDelta]` — query current positions
- `get_account_balance() -> dict` — query margin/balance
- `shutdown() -> None` — graceful disconnect

## 3. Per-Broker Submodule Structure

Each broker adapter lives under `src/hft_platform/feed_adapter/<broker>/`:

```
feed_adapter/
├── shioaji/
│   ├── __init__.py
│   ├── client.py          # Main client implementing BrokerProtocol
│   ├── session.py         # Login, token refresh, reconnect
│   ├── quote.py           # Market data subscription, callbacks
│   ├── order_gateway.py   # Order placement, cancellation, amendment
│   ├── account.py         # Position, margin, balance queries
│   └── contracts.py       # Contract/symbol resolution
├── fubon/
│   ├── __init__.py
│   ├── client.py
│   ├── session.py
│   ├── quote.py
│   ├── order_gateway.py
│   ├── account.py
│   └── contracts.py
├── protocol.py            # BrokerProtocol definition
├── factory.py             # BrokerFactory with @register decorator
└── normalizer.py          # Shared normalization (unchanged)
```

This structure mirrors the decomposition targets already identified in the Shioaji decoupling plan (Workstream B). Each module has a single responsibility and can be tested independently.

## 4. Data Flow (Inbound — Market Data Feed)

```
Broker SDK callback
  → feed_adapter/<broker>/quote.py (raw payload)
  → MarketDataService (src/hft_platform/services/market_data.py)
  → normalizer.py → TickEvent / BidAskEvent
  → LOBEngine → FeatureEngine (when enabled) → StrategyRunner
```

Key invariant: `quote.py` in each broker adapter must normalize the broker-specific callback payload into the platform's canonical raw format before handing off to `MarketDataService`. The shared `normalizer.py` then produces `TickEvent` and `BidAskEvent` objects that are broker-agnostic.

## 5. Data Flow (Outbound — Orders)

```
StrategyRunner → OrderIntent
  → RiskEngine → OrderCommand
  → OrderAdapter → BrokerOrderTranslator.translate_*(cmd)
  → feed_adapter/<broker>/order_gateway.py
  → Broker API
```

`BrokerOrderTranslator` is a per-broker helper that maps the platform's `OrderCommand` fields (scaled-int prices, internal symbol IDs) into the broker SDK's expected format. Each broker submodule provides its own translator.

## 6. Execution Callback Flow

```
Broker execution callback
  → raw_exec_queue (bounded async queue)
  → ExecutionRouter (src/hft_platform/execution/router.py)
  → ExecutionNormalizer (with BrokerExecFieldMap)
  → FillEvent / OrderEvent
  → PositionTracker → Recorder
```

`BrokerExecFieldMap` is a per-broker mapping that tells `ExecutionNormalizer` how to extract fill price, quantity, fees, tax, and order status from the broker's raw execution payload. This is a data-driven approach — no broker-specific `if/else` branches in `ExecutionRouter`.

## 7. Configuration Resolution

```
HFT_BROKER env var
  → config/base/brokers/{broker}.yaml
  → BrokerConfig (pydantic schema, per-broker)
  → BrokerFactory.create(broker_name, config) → BrokerProtocol instance
```

Per-broker config files are kept separate from `main.yaml` to avoid config pollution. Each broker YAML contains:

- Authentication credentials (references to env vars, not plaintext)
- API endpoints and transport settings
- Rate limits and circuit breaker thresholds
- Supported order types and features
- Latency profile reference (for research/backtest)

Example: `config/base/brokers/shioaji.yaml`, `config/base/brokers/fubon.yaml`.

## 8. Broker Comparison Table

| Feature | Shioaji (永豐金) | Fubon (富邦) |
|---------|-----------------|-------------|
| Transport | Proprietary SDK | HTTP / WebSocket |
| Auth | Certificate + API Key | API Key + Password |
| Market Data | Streaming callbacks | WebSocket subscription |
| L2 Depth | Yes (via BidAskEvent) | Top 5 bids/asks |
| Order API | SDK method calls | REST API |
| Batch Orders | No | Yes |
| Smart Orders | Touch orders | Conditional stop-loss, trailing |
| SDK Languages | Python | Python, C++, C#, Node.js, Go |
| Sim RTT (P95) | ~36ms | TBD (must measure) |

Notes:

- Fubon's L2 depth (top 5) is shallower than Shioaji's full book. Strategies relying on deep book features must account for this.
- Fubon's batch order support may enable more efficient order management for multi-leg strategies.
- Sim RTT for Fubon must be measured and recorded in `config/research/latency_profiles.yaml` before any alpha can be promoted on Fubon.

## 9. Key Design Decisions

| ID | Decision | Rationale |
|------|----------|-----------|
| D-MB-01 | Use `typing.Protocol` (structural subtyping), not ABC | Existing `ShioajiClientFacade` satisfies the protocol without modification. No forced inheritance hierarchy. |
| D-MB-02 | `BrokerFactory` uses registry pattern with `@register` decorator | New brokers are added by decorating their client class. No central switch statement to maintain. |
| D-MB-03 | Per-broker config in separate YAML files (not merged into `main.yaml`) | Prevents config key collisions. Each broker's config schema can evolve independently. |
| D-MB-04 | Dual-broker mode (routing symbols to different brokers) is future scope | Adds routing complexity. Single-broker-per-instance is simpler to reason about and operate. |
| D-MB-05 | `BrokerExecFieldMap` for execution normalization (data-driven, not code-branched) | Keeps `ExecutionRouter` broker-agnostic. Adding a broker requires a field map, not code changes. |

## 10. Dual-Broker Deployment (Future Scope)

When D-MB-04 is implemented, a `BrokerRouter` will map symbols to brokers:

```
Symbols YAML → BrokerRouter → {
  "2330" → shioaji (primary, equities)
  "TXFC6" → fubon (futures)
}
```

This enables:

- Routing equities to one broker and futures to another based on fee structure or API capability.
- Failover routing (if primary broker is down, route to secondary).
- A/B execution quality comparison across brokers for the same symbol.

Implementation requirements for dual-broker mode:

1. `BrokerRouter` must be deterministic and auditable (log every routing decision).
2. Position accounting must be partitioned by broker to avoid cross-broker netting errors.
3. Risk limits must be enforced per-broker and globally.
4. Recorder must tag all events with `broker_id` for post-trade analysis.

## 11. Migration Path

### Phase 1: Protocol Definition and Shioaji Conformance

1. Define `BrokerProtocol` in `feed_adapter/protocol.py`.
2. Complete D2 (Shioaji decomposition) into submodules under `feed_adapter/shioaji/`.
3. Verify `ShioajiClient` structurally satisfies `BrokerProtocol`.
4. Add `BrokerFactory` with Shioaji as the only registered broker.
5. Wire `HFT_BROKER` env var through config loader.

### Phase 2: Fubon Adapter

1. Scaffold `feed_adapter/fubon/` submodules.
2. Implement Fubon session, quote, and order modules.
3. Measure and record Fubon latency profile.
4. Add `config/base/brokers/fubon.yaml`.
5. Integration test with Fubon sandbox.

### Phase 3: Dual-Broker (Future)

1. Implement `BrokerRouter` with symbol-to-broker mapping.
2. Partition position accounting by broker.
3. Add cross-broker risk aggregation.
4. Operational tooling for broker failover.

## 12. Architectural Invariants

1. Platform Core must never import from `feed_adapter/<broker>/` directly. All access goes through `BrokerProtocol`.
2. All price fields crossing the broker boundary must be scaled integers (x10000). Broker adapters handle conversion.
3. Each broker adapter must provide a latency profile before any alpha can be promoted on that broker.
4. `BrokerFactory.create()` must raise immediately if the requested broker is not registered (fail-fast).
5. Execution normalization must be data-driven via `BrokerExecFieldMap`, not broker-specific code branches.
