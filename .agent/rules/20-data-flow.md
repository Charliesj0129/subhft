# Data Flow Rules

## Hot Path (Latency-Critical)

```
ShioajiClient.callback (broker thread) → loop.call_soon_threadsafe(_enqueue_raw)
→ raw_queue (bounded asyncio.Queue) → MarketDataService.run()
→ normalizer.normalize_{tick,bidask} [Rust] → LOBEngine.process_event() [Rust]
→ FeatureEngine.process_lob_stats() → FeatureUpdateEvent [27 features v3]
→ RingBufferBus.publish_nowait() → StrategyRunner.process_event()
→ strategy.handle_event() → OrderIntent[] → risk_queue / LocalIntentChannel
→ GatewayService (if enabled) or RiskEngine.evaluate()
→ OrderAdapter._api_queue.put_nowait(OrderCommand) → ShioajiClient.place_order()
```

Full hop-by-hop trace: `docs/architecture/pipeline-chains.md`.

## Recording Path (Parallel, Non-Blocking)

```
MarketDataService._record_direct_event() → recorder_queue.put_nowait() [drops on full]
→ RecorderService.run() → Batcher → DataWriter → ClickHouse INSERT (or WAL if wal_first)
```

## Verification After Changes

1. Metrics: `curl http://localhost:9090/metrics | grep hft_`
2. CH: `SELECT count() FROM hft.market_data WHERE toDate(exch_ts/1e9) = today()`
3. WAL: check `.wal/` for new files if CH disabled.
4. Queue depths: `raw_queue_depth`, `gateway_intent_channel_depth`.
5. Latency histograms: `normalize_latency_ns`, `lob_process_latency_ns`, `strategy_latency_ns`.

## Invariants

- Recording MUST NEVER block the hot path; use `put_nowait()` with drop policy.
- Normalizer output is always scaled int (x10000); no raw float prices downstream.
- Timestamps: `timebase.now_ns()`, never `datetime.now()`.
- Broker callbacks run in a separate thread; MUST use `call_soon_threadsafe()` to cross into event loop.
