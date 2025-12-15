# Plan – LOB Engine

## Components
1. **Data Structures**
   - Per-symbol `BookState`: arrays for bids/asks (top-5), metadata fields (last trade, mid, spread, imbalance, timestamps).
   - Use fixed-size lists or numpy array for performance; provide view for strategies.
2. **Snapshot Handler**
   - Accept normalized snapshot events, rebuild book state atomically, increment version.
3. **Incremental Handler**
   - Process `BidAsk` events: update levels, recompute derived stats, log anomalies.
4. **Tick Handler**
   - Update last trade info, compute trade-based metrics (price change, last aggressor).
5. **Feature Computation**
   - Mid = (best_bid + best_ask)/2.
   - Spread = best_ask - best_bid.
   - Imbalance = (sum_bid_vol - sum_ask_vol)/(sum_bid_vol + sum_ask_vol).
   - Depth totals per side, queue deltas using diff volumes.
   - Expose via `get_features(symbol)` and optional bus events.
6. **Concurrency Strategy**
   - Single writer updates; strategies read from snapshot caches.
   - Provide read-only view (copy) or memoryview pointer; update stats under lock.
7. **Integration with Strategy Context**
   - StrategyRunner obtains per-symbol `BookState`/features reference for context.
8. **Observability**
   - Metrics: updates per second, snapshot count, degraded symbols.
   - Logs: warnings for negative volumes, timestamp regressions.

## Implementation Steps
1. Design `BookState` class with methods for apply_snapshot/update_bidask/update_tick/recompute_features.
2. Refactor existing `LOBEngine` to manage dictionary of `BookState`.
3. Add strategy-facing API (`get_book(symbol)`, `get_features(symbol)`).
4. Implement feature event emission (optional) and integrate with event bus.
5. Add metrics and logging hooks.
6. Update StrategyRunner to use LOB data.
7. Write unit tests for update scenarios and feature correctness.
8. Document LOB structure & feature definitions.
