<!-- Generated: 2026-03-30 | Files scanned: 312 | Token estimate: ~950 -->

# Data Codemap

## Event Types (events.py)

| Event | Key Fields | Scale | Hot Path |
|-------|-----------|-------|----------|
| TickEvent | symbol, price, volume, trade_direction, trade_confidence | price x10000 | Yes |
| BidAskEvent | symbol, bids/asks (ndarray N x 2), is_snapshot, stats | prices x10000 | Yes |
| LOBStatsEvent | symbol, mid_price_x2, spread_scaled, imbalance, best_bid/ask, depth | x10000 | Yes |
| FeatureUpdateEvent | symbol, feature_set_id, changed_mask, feature_ids, values | per-spec | Yes |

## Contracts (contracts/)

| Contract | File | Key Fields | Direction |
|----------|------|-----------|-----------|
| OrderIntent | strategy.py | intent_id, strategy_id, symbol, side, price(x10000), qty, tif, idempotency_key, ttl_ns | Strategy → Risk |
| RiskDecision | strategy.py | approved, intent, reason_code | Risk → Gateway |
| OrderCommand | strategy.py | cmd_id, intent, deadline_ns, storm_guard_state | Risk → Order |
| OrderEvent | execution.py | order_id, status(0-5), filled_qty, price(x10000) | Execution → Bus |
| FillEvent | execution.py | fill_id, order_id, side, qty, price(x10000), fee(x10000), tax(x10000) | Execution → Bus |
| PositionDelta | execution.py | account_id, strategy_id, symbol, net_qty, avg_price(x10000), realized_pnl(x10000) | Execution → Bus |

## Enums

```
Side: BUY=0, SELL=1
TIF: LIMIT=0, IOC=1, FOK=2, ROD=3
IntentType: NEW=0, AMEND=1, CANCEL=2, FORCE_FLAT=3
OrderStatus: PENDING_SUBMIT=0, SUBMITTED=1, PARTIALLY_FILLED=2, FILLED=3, CANCELLED=4, FAILED=5
StormGuardState: NORMAL=0, WARM=1, STORM=2, HALT=3
```

## Typed Aliases (zero-cost)

```
ScaledPrice = NewType("ScaledPrice", int)   # x10000
ScaledPnl = NewType("ScaledPnl", int)       # x10000
ScaledFee = NewType("ScaledFee", int)       # x10000
```

## ClickHouse Tables (migrations/clickhouse/)

| Migration | Table(s) | Purpose |
|-----------|----------|---------|
| 20260301_001 | market_data, orders, trades, ohlcv_1m (MV) | Core schema |
| 20260302_001 | (TTL policies) | 6-month retention |
| 20260312_001 | ohlcv_1m | OHLCV aggregation fix |
| 20260319_001 | pnl_snapshots | Periodic position snapshots |
| 20260320_001 | shadow_orders | Shadow mode logging |
| 20260323_001 | reconciliation | Recon mismatches |
| 20260325_001 | slippage_records | TCA slippage decomposition |
| 20260325_002 | (alter trades) | tax_scaled field (x10000) |
| 20260327_001 | config_snapshots | Configuration audit trail |
| 20260327_002 | (alter trades) | decision_price, arrival_price TCA |
| 20260327_003 | daily_reports | Daily PnL/risk reports |
| 20260327_004 | liquidity_gate_events | Liquidity gate state transitions |
| 20260328_001 | (alter market_data) | EMO trade direction classification |
| 20260330_001 | (alter market_data, orders, fills) | Multi-instrument: instrument_type, underlying, strike, option_right, expiry |
| 20260401_001 | wal_dedup | WAL replay deduplication tracking |

## Rust Boundary (rust_core via PyO3)

### LOB + Normalization
scale_book, scale_book_seq, scale_book_pair, scale_book_pair_stats, scale_book_pair_stats_np,
compute_book_stats, get_field, normalize_tick_tuple, normalize_bidask_tuple,
normalize_bidask_tuple_np, normalize_bidask_tuple_with_synth, normalize_tick_v2, normalize_bidask_v2

### Event Routing
EventBus, FastRingBuffer, FastTickRingBuffer, FastBidAskRingBuffer, FastLOBStatsRingBuffer,
FastTypedRingBuffer (W4)

### Feature Engine
LobFeatureKernelV1, RustFeaturePipelineV1, RustFeatureEngineV2

### Risk + Execution
FastGate, RustRiskValidator, RustExposureStore, RustCircuitBreaker, RustStormGuardValidator,
RustDedupStore, RustGatewayFusedCheck (W4)

### Alpha Signals
AlphaDepthSlope, AlphaOFI, AlphaRegimePressure, AlphaRegimeReversal,
AlphaTransientReprice, AlphaMarkovTransition, MatchedFilterTradeFlow, MetaAlpha, AlphaStrategy

### State + Position
LimitOrderBook, RustBookState, RustPositionTracker, SymbolInternTable (W4)

### Persistence
RustColumnarBuffer, RustMetricsSampler, to_ch_price_scaled,
map_tick_record, map_bidask_record, map_order_record, map_fill_record

### IPC
ShmRingBuffer, ShmSnapshotTable

### Fused Pipelines (W4)
RustNormalizerLobFused, RustNormalizerFeatureFusedV1

### Utilities
coerce_ns_int, coerce_ns_float, signals_to_positions, apply_latency_to_positions

## Persistence Topology

```
Hot Path Events → recorder_queue (put_nowait, drop on full)
  → RecorderService.run()
    → Batcher.add() → check_flush()
      → DataWriter
        ├─ direct mode: ClickHouse INSERT (clickhouse-connect)
        └─ wal_first mode: WAL file → WALLoaderService → ClickHouse
```

## WAL Durability

```
WALWriter → .wal/<topic>/<shard>.wal (append-only, fcntl lock)
WALLoaderService → batch read → ClickHouse INSERT → mark consumed
Shard claim: fcntl-based exclusive ownership for multi-loader scale-out
Disk pressure: OK → WARN → CRITICAL → HALT (daemon monitor)
```
