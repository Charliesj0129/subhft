# momentum_decay

Short-term momentum decay alpha. The ratio of fast EMA to slow EMA of price
changes reveals whether momentum is building or fading.

## Formula

```
MD_t = EMA_4(delta_P) / max(|EMA_32(delta_P)|, epsilon) - sign(EMA_32(delta_P))
```

## Data Fields

- `mid_price` (scaled int from LOBStatsEvent)

## Status

DRAFT
