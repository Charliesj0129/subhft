# flow_mode_decomp

**Activity-Normalised Directional Flow Mode**

## Paper Reference

- **2405.10654** — "Microstructure Modes – Disentangling the Joint Dynamics of Prices & Order Flow"
- Elomari-Kessab, Maitrier, Bonart, Bouchaud (CFM / École Polytechnique, 2024)

## Signal

```
A_t = Δbid_qty_t - Δask_qty_t                          # anti-symmetric mode
activity_t = |Δbid_qty_t| + |Δask_qty_t| + 1           # symmetric activity
FMD_t = EMA_32( A_t / activity_t )                      # normalised signal
```

## Hypothesis

Normalising directional order-flow changes by total LOB activity isolates the
anti-symmetric microstructure mode. This mode carries return-predictive information
that raw OFI or level-based imbalance measures miss during high-activity periods.

## Data Fields

`bid_qty`, `ask_qty` (L1 only)

## Complexity

O(1) per tick — scalar EMA with 6 state variables (`__slots__`).

## Backtest Results (2026-03-16)

Config: EMA_32, pos_scale=0.3, pos_step=0.10, latency=36ms, fees=±0.2bps

| Symbol | Days | Sharpe | IC     | MDD    | Win Rate |
|--------|------|--------|--------|--------|----------|
| 2330   | 6    | 31.42  | +0.019 | 0.000  | 100%     |
| 2881   | 19   | 16.91  | +0.057 | -0.000 | 89%      |
| 2454   | 19   | 30.91  | +0.053 | 0.000  | 100%     |
| 2317   | 18   | 26.14  | +0.034 | -0.004 | 94%      |

Gate C: PASS (4/4) | Gate D: PASS (4/4)

Correlation vs promoted alphas: FMD↔QI < 0.17, FMD↔MM < 0.37 (orthogonal).
