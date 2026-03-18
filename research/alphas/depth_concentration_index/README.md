# depth_concentration_index

## Signal

```
HHI_side = sum_i ( depth_i / total_depth )^2
signal   = EMA_16( HHI_ask - HHI_bid )
```

## Hypothesis

The Herfindahl-Hirschman Index of LOB depth distribution across price levels
reveals liquidity fragility.  When ask-side depth is concentrated at a single
level (high HHI), aggressive buying can punch through quickly, generating
upward price pressure.  The asymmetry `HHI_ask - HHI_bid` is a directional
predictor of short-term mid-price movement.

## Data Fields

- `bids` — multi-level bid array, shape (N, 2): col 0 = price, col 1 = qty
- `asks` — multi-level ask array, shape (N, 2): col 0 = price, col 1 = qty

## State (3 slots)

| Slot           | Type  | Purpose                          |
| -------------- | ----- | -------------------------------- |
| `_ema`         | float | EMA of HHI asymmetry             |
| `_signal`      | float | Last emitted signal              |
| `_initialized` | bool  | Whether first tick has been seen |

## Design Notes

- HHI range: [1/N, 1.0] where N = number of levels.
  - 1.0 = all depth at one level (maximum concentration / fragility)
  - 1/N = evenly distributed (minimum concentration / maximum resilience)
- Signal range: approximately [-1, 1] (difference of two HHIs).
- EMA window = 16 ticks smooths noise while preserving reactivity.
- O(L) per update where L = number of LOB levels (typically 5).
- All state is scalar (Allocator Law compliance).

## References

- Kyle (1985): Continuous auctions and insider trading
- Cont, Stoikov & Talreja (2010): A stochastic model for order book dynamics
- Huang & Polak (2011): LOB depth concentration and intraday volatility
