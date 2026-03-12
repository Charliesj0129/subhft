# price_level_revert

## Signal

`-EMA_16((mid_price_x2 - EMA_128(mid_price_x2)) / max(spread_scaled, 1))`

## Hypothesis

Price deviation from a longer-term moving average (128-tick EMA), normalized by the
current spread, is mean-reverting. The negative sign fades the deviation: when price
is above the slow EMA the signal is negative (sell pressure), when below it is
positive (buy pressure).

This differs from `microprice_reversion` which uses the microprice-mid gap.

## Data Fields

- `mid_price_x2` — twice the mid price (scaled integer from `LOBStatsEvent`)
- `spread_scaled` — spread in scaled integer units (from `LOBStatsEvent`)

## State (4 slots)

| Slot           | Type    | Description                              |
| -------------- | ------- | ---------------------------------------- |
| `_mid_ema128`  | `float` | Slow EMA of `mid_price_x2` (128-tick)    |
| `_dev_ema16`   | `float` | Fast EMA of normalized deviation (16-tick)|
| `_signal`      | `float` | Cached output (negated `_dev_ema16`)     |
| `_initialized` | `bool`  | Whether first tick has been processed    |

## EMA Coefficients

- `_EMA_ALPHA_16  = 1 - exp(-1/16)  ≈ 0.0606`
- `_EMA_ALPHA_128 = 1 - exp(-1/128) ≈ 0.0078`

## Gate Status

- **Status**: DRAFT
- **Tier**: TIER_2
- **Latency profile**: `shioaji_sim_p95_v2026-03-04`
