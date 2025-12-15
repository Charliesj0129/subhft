# Plan – MarketDataNormalizer

## Components
1. **Symbol Metadata Loader**
   - Source: `config/symbols.yaml`.
   - Logic: Resolve `price_scale` from `decimals` or `tick_size`.
   - Default: `10000` scale if unspecified.

2. **Normalizer Core (`feed_adapter/normalizer.py`)**
   - **Helpers**: `_coalesce` (handle Case variations), `_scale_price`, `_to_int`, `capture_local_time_ns`.
   - **Tick**: Map fields from `sinotrade_tutor_md/market_data/streaming/stocks.md`.
   - **BidAsk**: Map array fields `BidPrice`/`BidVolume`.
   - **Snapshot**: Map scalar BBO fields from `sinotrade_tutor_md/market_data/snapshot.md`.

3. **Observability**
   - Metric: `normalization_errors_total` (Counter).
   - Logging: `structlog` with sample payload on error.

4. **Testing**
   - Unit tests mocking raw payloads (both PascalCase and snake_case).
   - Verification of `price_scale` logic.

## Implementation Steps
1. [x] Implement `SymbolMetadata` loader.
2. [x] Implement `MarketDataNormalizer` skeleton & sequence logic.
3. [x] Add `normalize_tick` with coalescing and scaling.
4. [x] Add `normalize_bidask` with array handling.
5. [x] Add `normalize_snapshot` with scalar BBO support (Spec Correction).
6. [x] Integrate Metrics (`normalization_errors_total`).
7. [ ] Create checklist & run final verification.

## References
- `sinotrade_tutor_md` directory for field definitions.
