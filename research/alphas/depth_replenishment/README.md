# depth_replenishment

## Hypothesis
- The speed at which LOB depth recovers after depletion indicates market maker
  confidence. Fast replenishment on one side signals expected price move away
  from that side.

## Formula
- `DR_t = EMA_8(delta_total_depth) * sign(bid_qty - ask_qty)`
- `delta_total_depth = (bid_qty + ask_qty) - prev(bid_qty + ask_qty)`

## Metadata
- `alpha_id`: `depth_replenishment`
- `paper_refs`: (none)
- `complexity`: `O(1)`
- `data_fields`: `("bid_qty", "ask_qty")`
