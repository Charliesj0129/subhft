# queue_acceleration

## Hypothesis
- Queue imbalance at the best levels provides a fast proxy for one-tick directional pressure when spread remains stable.

## Formula
- `• **Master Parameter $H_0$**: Core Flow 的持久性指數。實證估計 $H_0 \approx 3/4 (0.75)$。`

## Data Fields
- `l1_bid_qty`
- `l1_ask_qty`
- `l1_imbalance_ppm`
- `spread_scaled`
- `spread_ema8_scaled`
- `mid_price_x2`
- `depth_imbalance_ppm`
- `depth_imbalance_ema8_ppm`
- `microprice_x2`

## Metadata
- `alpha_id`: `queue_acceleration`
- `paper_refs`: 026
- `complexity`: `O(1)`
