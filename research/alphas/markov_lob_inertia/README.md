# markov_lob_inertia

**Paper Ref**: 039 — Intraday LOB Price Change Markov Dynamics

## Signal

Tracks a 3-state Markov chain of price movements (UP/DOWN/FLAT).
Transition probabilities reveal price inertia — tendency to continue
in the same direction. High inertia amplifies momentum signals;
low inertia signals mean-reversion opportunity.

## Formula

```
state     = classify(mid_price - prev_mid) -> {DOWN=0, FLAT=1, UP=2}
T[i,j]   += decay * (indicator(i->j) - T[i,j])   (EMA-smoothed)
p_up      = T[state, UP]   / row_sum
p_down    = T[state, DOWN] / row_sum
inertia   = T[state, state] / row_sum
raw       = (p_up - p_down) * inertia
signal   += alpha_ema * (raw - signal)             in [-1, 1]
```

## Data Fields

- `bid_qty`, `ask_qty` (required)
- `mid_price` (optional; if absent, uses imbalance proxy)

## Complexity

O(1) per tick — 3x3 transition matrix update via EMA decay.

## Status

DRAFT
