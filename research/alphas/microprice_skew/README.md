# microprice_skew

Microprice skew alpha: normalized deviation of volume-weighted midpoint from simple midpoint.

## Signal

```
MS_t = EMA_8((microprice - mid_price) / max(spread, epsilon))
```

Where `microprice = (ask_px * bid_qty + bid_px * ask_qty) / max(bid_qty + ask_qty, epsilon)`.

## Hypothesis

The microprice deviates from the simple midpoint when there is depth asymmetry.
The normalized deviation measures information asymmetry and predicts price direction.
Positive signal = microprice above mid (buy pressure), negative = below (sell pressure).

## Data Fields

`bid_px`, `ask_px`, `bid_qty`, `ask_qty`, `mid_price`

## Status

DRAFT
