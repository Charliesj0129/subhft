# Market Data Normalizer Checklist

## External Spec Compliance
- [ ] **Tick Fields**: Validated against `sinotrade_tutor_md/market_data/streaming/stocks.md`.
    - `Close`, `Volume`, `Amount`, `TotalVolume` mapped correctly.
    - `TickType`, `Simtrade` handled.
- [ ] **BidAsk Fields**: Validated against `sinotrade_tutor_md/market_data/streaming/stocks.md`.
    - `BidPrice`/`AskPrice` arrays processed.
    - `DiffBidVol` handled if present.
- [ ] **Snapshot Fields**: Validated against `sinotrade_tutor_md/market_data/snapshot.md`.
    - Scalar `buy_price` / `sell_price` (BBO) supported.
    - `open`, `high`, `low`, `close` supported.
- [ ] **Limits**: Timestamp precision (ns) and Scaling (int) enforced.

## Internal Logic
- [ ] **Metadata**: `SymbolMetadata` loads `config/symbols.yaml`.
- [ ] **Scaling**: `price_scale` used for all price fields.
- [ ] **Coalescing**: `_coalesce` handles PascalCase/snake_case variations.
- [ ] **Safety**: `try/except` block surrounds normalization; errors logged.
- [ ] **Metrics**: `normalization_errors_total` incremented on failure.
- [ ] **Sequence**: `seq` is monotonic and thread-safe.

## Verification
- [ ] Unit tests pass for Tick (v0/v1).
- [ ] Unit tests pass for BidAsk.
- [ ] Unit tests pass for Snapshot (BBO).
