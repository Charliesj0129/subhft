# flow_toxicity_ratio

## Signal

```
FTR_t = EMA_16( |ofi_l1_raw| / max(l1_bid_qty + l1_ask_qty, 1) )
```

## Hypothesis

Ratio of absolute OFI to total L1 queue depth measures flow toxicity.
High ratio indicates informed traders consuming disproportionate liquidity
regardless of direction. Unlike directional toxic-flow signals, this is
**unsigned** (magnitude only) — it measures toxicity *level*, not which side.

## Data Fields

- `ofi_l1_raw` — raw Level-1 order flow imbalance
- `l1_bid_qty` — Level-1 bid queue depth
- `l1_ask_qty` — Level-1 ask queue depth

## State

| Slot              | Type    | Description                    |
| ----------------- | ------- | ------------------------------ |
| `_toxicity_ema`   | `float` | EMA-16 of raw toxicity ratio   |
| `_signal`         | `float` | Current signal value           |
| `_initialized`    | `bool`  | Whether first tick has arrived  |

## Status

DRAFT — Gate A/B pending.
