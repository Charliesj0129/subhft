# queue_acceleration

## Hypothesis
- The acceleration (second derivative) of queue imbalance detects inflection points where directional pressure is changing.

## Formula
- `qi = (bid - ask) / max(bid + ask, 1)`
- `velocity = EMA_8(qi) - EMA_32(qi)`
- `accel = velocity - prev_velocity`
- `signal = clip(EMA_4(accel), -1, 1)`

## Data Fields
- `bid_qty`
- `ask_qty`

## Metadata
- `alpha_id`: `queue_acceleration`
- `paper_refs`: 026
- `complexity`: `O(1)`
