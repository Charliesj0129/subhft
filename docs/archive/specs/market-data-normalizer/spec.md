# MarketDataNormalizer – Spec

## Problem Statement
Convert raw Shioaji payloads (Tick/BidAsk/Snapshot) into normalized market events with consistent schemas, timestamping, and scaling.
**Reference Sources**:
- **Tick/BidAsk**: `sinotrade_tutor_md/market_data/streaming/stocks.md` (and related futures/options docs).
- **Snapshot**: `sinotrade_tutor_md/market_data/snapshot.md` (specifically level-1 scalars).
- **Limits**: `sinotrade_tutor_md/limit.md`.

## Requirements

### Inputs
- **Tick**: Dict with keys `Code`, `Datetime`, `Close`, `Volume`, `Simtrade` (or lowercase variants v0).
- **BidAsk**: Dict with keys `Code`, `Datetime`, `BidPrice`, `BidVolume`, `AskPrice`, `AskVolume`.
- **Snapshot**: API response object (or dict) with `buy_price`, `sell_price` scalar BBO logic.
- **Metadata**: Loaded from `config/symbols.yaml` (`tick_size`, `decimals`, `price_scale`, `odd_lot`).

### Outputs
Normalized events (`Tick`, `BidAsk`, `Snapshot`) must contain:
- `symbol` (str)
- `seq` (int, monotonic)
- `exchange_ts` (int, ns, source: `ts`/`datetime`)
- `local_ts` (int, ns, source: `time_ns`)
- **Scaled Values**: Prices converted to integers based on `SymbolMetadata`.
    - Default `price_scale`: 10,000.
    - `volume`: Integer units.

### Functional Requirements
1. **Timestamp Capture**
   - Capture `local_ts` immediately upon dequeuing.
   - parse `exchange_ts` from payload. Handle string/datetime conversions if needed.
2. **Decimal Scaling**
   - Use `SymbolMetadata` to determine scale factor.
   - `int(float(value) * scale)`. Handle `None`/Empty strings safely (`_to_int`, `_scale_price`).
3. **Payload Coalescing**
   - Support PascalCase (`Close`) and snake_case (`close`) to handle API version differences.
   - Use `_coalesce(payload, "Close", "close")` pattern.
4. **Snapshot Handling**
   - Handle scalar BBO (`buy_price`, `sell_price`) as defined in `snapshot.md`.
   - Fallback to `bids`/`asks` arrays if present.
5. **Metrics**
   - Increment `normalization_errors_total` on failure.
   - Log errors with payload sample (truncated).

### Non-Functional
- **Latency**: Minimal allocation (use slots/dicts efficiently).
- **Resilience**: Never crash on malformed payload; log & skip.
- **Thread Safety**: `seq` increment must be thread-safe (Locked).

## Deliverables
- `feed_adapter/normalizer.py`: Robust implementation with field coalescing.
- `observability/metrics.py`: Error counters.
- Tests validation against sample payloads from `sinotrade_tutor_md`.
