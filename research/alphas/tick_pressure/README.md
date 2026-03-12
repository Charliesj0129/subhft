# tick_pressure

## Hypothesis
Price tick direction weighted by L1 queue size relative to baseline captures
buying/selling pressure: large queue + upward tick = strong buying pressure,
large queue + downward tick = strong selling pressure.

## Formula
```
TP_t = EMA_8( sign(mid_t - mid_{t-1}) × (V_bid + V_ask) / max(EMA_64(V_bid + V_ask), 1) )
```

## Metadata
- `alpha_id`: `tick_pressure`
- `data_fields`: `mid_price_x2`, `l1_bid_qty`, `l1_ask_qty`
- `complexity`: `O(1)`
- `latency_profile`: `shioaji_sim_p95_v2026-03-04`
