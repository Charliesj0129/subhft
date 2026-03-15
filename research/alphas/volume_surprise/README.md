# volume_surprise

Directional volume surprise alpha signal.

## Formula

```
VS_t = (volume / EMA_32(volume) - 1) * sign(bid_qty - ask_qty)
```

## Hypothesis

Volume surprise (current volume relative to expected volume) signals
information arrival. Abnormally high volume precedes directional moves;
combined with bid/ask imbalance to determine direction.

## Data Fields

- `volume` — current tick volume
- `bid_qty` — best bid queue size
- `ask_qty` — best ask queue size
