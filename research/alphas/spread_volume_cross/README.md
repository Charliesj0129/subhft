# spread_volume_cross

Cross-feature alpha: spread compression x activity surprise x imbalance sign.

## Signal

```
SVC_t = EMA_8(-delta_spread_bps * activity_surprise * sign(imbalance))
```

Where:
- `delta_spread_bps = spread_bps_t - spread_bps_{t-1}`
- `activity = volume (if available) else |delta_bid_qty| + |delta_ask_qty|`
- `activity_surprise = activity / EMA(activity)` (ratio > 1 = spike)
- `sign(imbalance) = sign(bid_qty - ask_qty)`

## Hypothesis

When spread narrows (negative delta) AND activity spikes simultaneously,
information is being incorporated into price. The cross-product, signed
by order-book imbalance, predicts short-term direction.

Positive signal = informed buying pressure; negative = informed selling.

## Data Compatibility

On L1-only data (no trade volume), the signal uses queue-change magnitude
as an activity proxy. This ensures non-trivial signal output on quote-only
market data feeds.

## Gate Status

- Gate A: PASS
- Gate B: PASS (20/20 tests)
- Gate C: FAIL (signal active but weak on 2330 L1 data; needs tuning or L2 data)
