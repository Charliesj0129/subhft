# ADR: Multi-Broker Support (Shioaji + Fubon)

**Status**: Accepted
**Date**: 2026-03-11
**Authors**: Charlie

## Context

The platform was originally built exclusively for Shioaji (永豐金證券).
To reduce single-broker concentration risk and access Fubon's lower
commission tiers for certain instrument classes, we need a clean
abstraction that lets the runtime switch between brokers via
configuration.

## Decision

Introduce a **BrokerFacade** abstraction selected at startup by the
`broker` key in `config/base/main.yaml` (override: `HFT_BROKER` env
var).

### Design Principles

1. **Single active broker per process** -- no fan-out routing in v1.
2. **Identical interface contract** -- every facade exposes:
   - `login() -> None`
   - `subscribe(symbols: list[str]) -> None`
   - `place_order(cmd: OrderCommand) -> str`  (returns broker order id)
   - `cancel_order(order_id: str) -> None`
   - `on_tick(callback)`, `on_bidask(callback)`
3. **Config-driven selection** -- `config/base/main.yaml` gains a
   top-level `broker: shioaji | fubon` key.  Per-broker credentials
   live in env vars (`HFT_FUBON_*`, `SHIOAJI_*`).
4. **Field mapping isolation** -- each facade owns its own
   normalizer that maps broker-native fields to the platform's
   canonical `TickEvent` / `BidAskEvent` / `FillEvent` contracts.
5. **Latency profile per broker** -- research latency profiles
   (`config/research/latency_profiles.yaml`) carry a `broker` tag so
   Gate C / Gate D simulations use the correct RTT distribution.

### Runtime Selection Flow

```
bootstrap.py
  -> config.broker ("shioaji" | "fubon")
  -> BrokerFacadeFactory.create(config)
       -> ShioajiFacade(config)   # existing ShioajiClient, wrapped
       -> FubonFacade(config)     # new, fubon_sdk based
  -> inject into MarketData plane + Execution plane
```

### Configuration

```yaml
# config/base/main.yaml
broker: shioaji          # default

# config/env/fubon/main.yaml  (overlay)
broker: fubon
```

Environment variables for Fubon:

| Variable             | Purpose                          |
| -------------------- | -------------------------------- |
| `HFT_FUBON_CERT_PATH` | Path to Fubon API certificate  |
| `HFT_FUBON_ACCOUNT`   | Trading account ID             |
| `HFT_FUBON_PASSWORD`  | Account password (secret mgr)  |

### Impact on Existing Code

- `ShioajiClient` is **not modified** -- it is wrapped by
  `ShioajiFacade` which delegates all calls.
- `Normalizer` remains broker-agnostic; each facade normalizes to
  canonical events before publishing to the ring buffer.
- `OrderAdapter` receives `OrderCommand` regardless of broker; the
  facade translates to broker-native API calls.
- Risk engine, strategy runner, and recorder are **completely
  unaffected**.

## Consequences

### Positive

- Clean separation of broker-specific logic from platform core.
- New brokers can be added by implementing `BrokerFacade` protocol
  (< 200 LOC per broker).
- Research pipeline can simulate per-broker latency profiles.

### Negative

- Slight indirection cost at startup (factory pattern); zero
  hot-path overhead since the facade reference is resolved once.
- Two sets of integration tests needed (one per broker).

### Risks

- Fubon SDK has different error semantics; need careful mapping to
  platform error contracts.
- Quote schema differences may surface edge cases in normalizer.

## Related

- `docs/architecture/current-architecture.md` -- canonical architecture
- `config/research/latency_profiles.yaml` -- per-broker latency models
- `src/hft_platform/feed_adapter/shioaji_client.py` -- existing broker impl
