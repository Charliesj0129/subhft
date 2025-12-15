```
# Tasks – MarketDataNormalizer

| ID | Title | Description & Acceptance | Dependencies |
| --- | --- | --- | --- |
| N1 | Symbol metadata loader | Implement `SymbolMetadata` to load `config/symbols.yaml`. Support `tick_size` -> `price_scale` logic. **Acceptance**: Correct scale returned for `2330`. | — |
| N2 | Sequence/timestamp framework | Implement `_next_seq()` (Locked) and `capture_local_time_ns()`. | N1 |
| N3 | Tick normalization | Normalize Tick per `sinotrade_tutor_md/market_data/streaming/stocks.md`. Support PascalCase/snake_case via `_coalesce`. **Acceptance**: All fields from spec mapped and scaled. | N2 |
| N4 | BidAsk normalization | Normalize BidAsk arrays per `sinotrade_tutor_md`. Handle `BidPrice`/`BidVolume` lists. **Acceptance**: LOB levels correctly formed. | N2 |
| N5 | Snapshot normalization | Normalize Snapshot per `sinotrade_tutor_md/market_data/snapshot.md` (Scalar BBO). **Acceptance**: `buy_price`/`sell_price` mapped to `bids[0]`/`asks[0]`. | N3, N4 |
| N6 | Error handling & metrics | Integrate `MetricsRegistry` (`normalization_errors_total`). Wrap logic in try/except. | N3, N4 |
| N7 | Documentation & examples | Update `docs/feed_adapter.md` with final schema. Create `specs/market-data-normalizer/checklist.md`. | N3–N6 |
```
