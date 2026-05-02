# events — Canonical Event Types

> **File**: `src/hft_platform/events.py`
> **Runtime Plane**: Cross-cutting (event bus payload)

## Overview

Defines the 5 core event types flowing through `RingBufferBus`. All price fields use **scaled integers (x10000)**.

## Event Types

### TickEvent — Trade/Tick Updates

```python
@dataclass(slots=True)
class TickEvent:
    meta: MetaData              # seq, source_ts, local_ts, topic
    symbol: str
    price: int                  # Scaled x10000
    volume: int                 # Incremental
    total_volume: int = 0       # Cumulative
    bid_side_total_vol: int = 0
    ask_side_total_vol: int = 0
    is_simtrade: bool = False
    is_odd_lot: bool = False
    trade_direction: int = 0    # +1=BUY, -1=SELL, 0=UNKNOWN
    trade_confidence: int = 0   # x1000: 1000=at-quote, 800=inside, 500=tick-rule
```

### BidAskEvent — L1/L5 Order Book Levels

```python
@dataclass(slots=True)
class BidAskEvent:
    meta: MetaData
    symbol: str
    bids: np.ndarray | list     # Shape (N,2): [[price, volume], ...] dtype int64
    asks: np.ndarray | list
    stats: BookStats | None = None
    fused_stats: FusedBookStats | None = None
    is_snapshot: bool = False
```

### LOBStatsEvent — Derived LOB Metrics

```python
@dataclass(slots=True, frozen=True)
class LOBStatsEvent:
    symbol: str
    ts: int
    imbalance: float            # Ratio [-1, 1]
    best_bid: int               # Scaled x10000
    best_ask: int               # Scaled x10000
    bid_depth: int
    ask_depth: int
    mid_price_x2: int | None    # best_bid + best_ask (auto-computed)
    spread_scaled: int | None   # best_ask - best_bid (auto-computed)
```

**Properties** (backward-compat):
- `mid_price` → `float` (mid_price_x2 / 2.0)
- `mid_price_scaled` → `int` (mid_price_x2 // 2)
- `spread` → `float`

### FeatureUpdateEvent — Feature Vector Updates

```python
@dataclass(slots=True)
class FeatureUpdateEvent:
    symbol: str
    ts: int                     # Source timestamp (ns)
    local_ts: int
    seq: int
    feature_set_id: str
    schema_version: int
    changed_mask: int           # Bitmask of changed features
    warmup_ready_mask: int      # Bitmask of ready features
    quality_flags: int
    feature_ids: tuple[str, ...]
    values: tuple[int | float, ...]
```

### GapEvent — Overflow Signal

```python
@dataclass(slots=True)
class GapEvent:
    missed_count: int
    first_missed_seq: int
    last_missed_seq: int
    ts: int
```

## Supporting Types

| Type | Fields | Usage |
|------|--------|-------|
| `MetaData` | `seq`, `source_ts`, `local_ts`, `topic` | Common event header |
| `BookStats` | best_bid/ask, depths, `mid_price: float`, `spread: float` | Backward-compat float stats |
| `FusedBookStats` | best_bid/ask, depths, `mid_price_x2: int`, `spread_scaled: int` | Integer-only stats (hot path preferred) |

## Scaling Conventions

| Field | Scale | Hot-Path Tip |
|-------|-------|-------------|
| Prices (bid/ask/mid) | x10000 | Use `mid_price_x2` directly, divide by 2 only for display |
| `spread_scaled` | x10000 (already scaled) | Integer comparison, no conversion needed |
| `imbalance` | float [-1, 1] | Acceptable as bounded ratio |
| `trade_confidence` | x1000 | 1000=at-quote, 800=inside, 500=tick-rule |

## Dependencies

- `numpy` (ndarray for bid/ask arrays)
- No other hft_platform imports (leaf module)
