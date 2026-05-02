# contracts — Inter-Module Data Contracts

> **Package**: `src/hft_platform/contracts/`
> **Runtime Plane**: Cross-cutting (foundation layer)
> **Depended on by**: ~20+ modules

## Overview

Domain-driven data contracts defining the command/event boundaries between strategy, risk, and execution planes. All prices use **scaled integers (x10000)**.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `types.py` | `ScaledPrice`, `ScaledPnl`, `ScaledFee`, `PLATFORM_SCALE` | Branded NewType aliases for compile-time type safety |
| `strategy.py` | `Side`, `TIF`, `IntentType`, `StormGuardState`, `OrderIntent`, `RiskDecision`, `RiskFeedback`, `OrderCommand` | Trading intent and risk decision contracts |
| `execution.py` | `OrderStatus`, `OrderEvent`, `FillEvent`, `PositionDelta` | Execution event contracts |

## Data Flow

```
Strategy → OrderIntent → (Risk) → RiskDecision → OrderCommand → (Broker) → FillEvent → PositionDelta
```

## Key Contracts

### OrderIntent (Strategy → Risk)

```python
@dataclass(slots=True)
class OrderIntent:
    intent_id: int
    strategy_id: str
    symbol: str
    intent_type: IntentType    # NEW, AMEND, CANCEL, FORCE_FLAT
    side: Side                 # BUY=0, SELL=1
    price: int                 # Scaled x10000
    qty: int
    tif: TIF                   # LIMIT, IOC, FOK, ROD
    idempotency_key: str       # CE2-01 dedup key
    ttl_ns: int                # Expiry in nanoseconds
    decision_price: int        # LOB mid-price at signal time (TCA)
    price_type: str            # "LMT" or "MKT"
```

### FillEvent (Broker → Execution)

```python
@dataclass(slots=True)
class FillEvent:
    fill_id: str
    order_id: str
    strategy_id: str
    symbol: str
    side: Side
    qty: int
    price: int                 # Scaled x10000
    fee: int                   # Scaled x10000
    tax: int                   # Scaled x10000
    ingest_ts_ns: int          # Platform receive time
    match_ts_ns: int           # Broker match time
    decision_price: int        # TCA passthrough
    arrival_price: int         # TCA passthrough
```

### PositionDelta (Execution → Risk/UI)

```python
@dataclass(slots=True)
class PositionDelta:
    account_id: str
    strategy_id: str
    symbol: str
    net_qty: int
    avg_price: int             # Scaled x10000
    realized_pnl: int          # Scaled x10000
    unrealized_pnl: int        # Scaled x10000
    delta_source: str          # "FILL", "RECONCILE", "MARK"
```

## Enums

| Enum | Values | Usage |
|------|--------|-------|
| `Side` | BUY=0, SELL=1 | Order direction |
| `TIF` | LIMIT=0, IOC=1, FOK=2, ROD=3 | Time-in-force |
| `IntentType` | NEW=0, AMEND=1, CANCEL=2, FORCE_FLAT=3 | Order lifecycle |
| `StormGuardState` | NORMAL=0, WARM=1, STORM=2, HALT=3 | Circuit breaker FSM |
| `OrderStatus` | PENDING_SUBMIT=0..FAILED=5 | Order lifecycle status |

## Precision Convention

- `PLATFORM_SCALE = 10_000`
- Example: 100.50 NTD → `1_005_000` (int)
- All `price`, `fee`, `tax`, `pnl` fields are scaled integers
- `ScaledPrice`, `ScaledPnl`, `ScaledFee` are zero-cost NewTypes (erased at runtime)

## Gotchas

1. **`decision_mid` is deprecated** — use `decision_price` for TCA
2. **`RiskFeedback` is frozen** — immutable for distributed processing safety
3. **Two timestamp fields** on events: `ingest_ts_ns` (platform) vs `broker_ts_ns`/`match_ts_ns` (broker) for latency analysis
4. **All dataclasses use `slots=True`** — memory optimization for high-frequency event processing
