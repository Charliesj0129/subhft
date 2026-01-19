# feed_adapter

## Purpose
Connect to market data sources and normalize raw payloads into internal events.

## Key Files
- `src/hft_platform/feed_adapter/shioaji_client.py`: Broker API client and subscriptions.
- `src/hft_platform/feed_adapter/normalizer.py`: `SymbolMetadata` and `MarketDataNormalizer`.
- `src/hft_platform/feed_adapter/lob_engine.py`: LOB reconstruction and stats.

## Data Flow
1) Client receives raw payloads (tick/bidask/snapshot).
2) Normalizer scales and converts to `TickEvent`/`BidAskEvent`.
3) LOB engine updates book and emits `LOBStatsEvent`.

## Configuration
- `SYMBOLS_CONFIG` points to the symbols file.
- `config/symbols.yaml` or `config/base/symbols.yaml` defines scale/tick size.

## Extension Points
- Add a new client for another data source.
- Add a new normalizer for different payload formats.
