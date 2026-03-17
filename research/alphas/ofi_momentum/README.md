# ofi_momentum

**OFI Level + Acceleration (MACD of Order Flow)**

## Paper Reference

- **0906.1444** — "High frequency market microstructure noise estimates and liquidity measures"
- Aït-Sahalia & Yu (2009) — noise-to-signal ratio at different timescales

## Signal

```
fast_OFI = EMA_8( (Δbid_qty - Δask_qty) / (|Δbid_qty| + |Δask_qty| + 1) )
slow_OFI = EMA_32( same )
OFIM_t = 0.5 * slow_OFI + 0.5 * (fast_OFI - slow_OFI)
```

## Hypothesis

The acceleration of order flow (change in OFI intensity) contains predictive
information beyond the OFI level. When informed flow is intensifying
(fast > slow), the next price move is more likely directional.

## Data Fields

`bid_qty`, `ask_qty` (L1 only)

## Complexity

O(1) per tick — dual EMA with 7 state variables (`__slots__`).

## IC Improvement

blend=0.5 gives mean IC = +0.055 across 4 symbols (+34% vs plain OFI +0.041).
