# LOB Engine – Spec

## Problem Statement
Maintain per-symbol top-of-book (top-5 levels) derived from normalized Tick/BidAsk/Snapshot events and provide strategies with low-latency access to current book state and derived features (mid, spread, imbalance, queue deltas). The engine must support shared memory updates, handle snapshot rebuilds, ensure monotonic timestamps, and emit feature events onto the bus.

## Requirements
### Inputs
- Normalized `BidAsk`, `Tick`, and `Snapshot` events from `MarketDataNormalizer`.
- Timer ticks for housekeeping (optional).

### State
- Per-symbol L2 ladder (top-5 bids/asks).
- Metadata: last trade, last snapshot version, exchange/local timestamps, derived stats.
- Version counter for snapshot vs incremental updates.

### Functional Requirements
1. **Snapshot Application**
   - Apply full snapshot events atomically: reset ladders, set version, emit `L2Snapshot` event for strategies/recorder.
2. **Incremental Updates**
   - For each `BidAsk`, update appropriate levels (full replace for v1 arrays).
   - Track diff volumes, queue changes, and last update timestamps.
3. **Tick Handling**
   - Update last trade price/volume, tick direction, and accumulate LOB-derived metrics (e.g., imbalance).
4. **Derived Features**
   - Compute mid-price, spread (ticks), volume imbalance, depth sums, queue deltas.
   - Provide API for strategies to read features (shared pointer/reference).
   - Optionally emit `MarketFeature` events to bus.
5. **Concurrency & Access**
   - Single writer (feed thread) updates state; multiple readers (strategies) require lock-free snapshots (e.g., copy-on-write or RCU).
6. **Validation & Monotonicity**
   - Reject out-of-order timestamps (log warning) or mark symbol as degraded.
   - Validate price ordering (bid <= ask) and volume non-negative.

### Non-Functional
- Latency: update per event <5 µs.
- Memory: per-symbol fixed-size structure (top-5 arrays, derived stats).
- Observability: metrics for update counts, snapshot versions, degradation events.
- Configurability: support symbol-level settings (depth, tick size from metadata).

### Deliverables
- Updated `feed_adapter/lob_engine.py` implementing above logic.
- StrategyContext integration for LOB/feature access.
- Tests covering snapshot application, bid/ask updates, feature calculations.
- Documentation describing LOB schema and feature definitions.
