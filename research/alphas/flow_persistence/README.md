# flow_persistence

## Hypothesis
- Order flow exhibits persistence (autocorrelation). When OFI maintains direction for sustained periods, the trend is likely to continue.

## Formula
- `OFI_raw = bid_qty - ask_qty`
- `ema_ofi = EMA_8(OFI_raw)`
- `ema_abs = EMA_16(|OFI_raw|)`
- `FP_t = ema_ofi * |ema_ofi| / max(ema_abs, epsilon)`

## Data Fields
- `bid_qty`
- `ask_qty`

## Metadata
- `alpha_id`: `flow_persistence`
- `complexity`: `O(1)`
