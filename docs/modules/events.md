# events

## Purpose

Internal event dataclasses for market data and LOB stats. These are the primary data objects flowing through the hot path.

## Key Files

- `src/hft_platform/events.py`: All event definitions.

## Event Types

| Event           | Fields                                                      | Produced By | Consumed By                   |
| --------------- | ----------------------------------------------------------- | ----------- | ----------------------------- |
| `TickEvent`     | `symbol, price (scaled int), volume, exch_ts, meta`         | Normalizer  | Strategy, Recorder            |
| `BidAskEvent`   | `symbol, bids[], asks[], is_snapshot, meta`                 | Normalizer  | LOBEngine, Strategy, Recorder |
| `LOBStatsEvent` | `symbol, mid_price, spread, bid_depth, ask_depth, ofi, ...` | LOBEngine   | Strategy                      |
| `EventMeta`     | `seq, topic, local_ts, source_ts`                           | Normalizer  | All                           |

## Price Convention

- **ALL prices are scaled integers** (x10000 by default, per `SymbolMetadata.price_scale`).
- `TickEvent.price = 1005000` means `$100.50` (if scale=10000).
- BidAsk `bids`/`asks` are lists of `(price_scaled, volume)` tuples.

## Gotchas

- `BidAskEvent.bids` or `.asks` can be `None` for one-sided quotes (pre-market).
- `EventMeta.seq` is the broker sequence number, NOT a local counter.
- These events are distinct from execution events (`FillEvent`, `OrderEvent`) in `contracts/`.
