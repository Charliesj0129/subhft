# Data Flow Rules

## Hot Path (Latency-Critical)

```
ShioajiClient.callback (broker thread)
→ loop.call_soon_threadsafe(_enqueue_raw)
→ raw_queue (asyncio.Queue, bounded)
→ MarketDataService.run() main loop
→ normalizer.normalize_{tick,bidask} [Rust fast path]
→ LOBEngine.process_event() [Rust fast path]
→ FeatureEngine.process_lob_stats() → FeatureUpdateEvent [27 features v3]
→ RingBufferBus.publish_nowait()
→ StrategyRunner.process_event()
→ strategy.handle_event() → OrderIntent[]
→ risk_queue.put_nowait() / LocalIntentChannel
→ GatewayService (if enabled) or RiskEngine.evaluate()
→ OrderAdapter._api_queue.put_nowait(OrderCommand)
→ ShioajiClient.place_order()
```

> **Full pipeline trace with hop-by-hop detail**: see `docs/architecture/pipeline-chains.md`

## Recording Path (Parallel, Non-Blocking)

```
MarketDataService._record_direct_event()
→ recorder_queue.put_nowait() [drops on full, enters degraded mode]
→ RecorderService.run()
→ Batcher.add() → Batcher.check_flush()
→ DataWriter → ClickHouse INSERT (or WAL file if wal_first mode)
```

## Verification After Changes

1. **Metrics**: `curl http://localhost:9090/metrics | grep hft_`
2. **ClickHouse**: `SELECT count() FROM hft.market_data WHERE toDate(exch_ts/1e9) = today()`
3. **WAL**: Check `.wal/` directory for new files if ClickHouse is disabled.
4. **Queue Depths**: Watch `raw_queue_depth`, `gateway_intent_channel_depth` metrics.
5. **Latency**: Check `normalize_latency_ns`, `lob_process_latency_ns`, `strategy_latency_ns` histograms.

## Invariants

- Recording MUST NEVER block the hot path. Use `put_nowait()` with drop policy.
- Normalizer output is always scaled integers (x10000). Never pass raw float prices downstream.
- Event timestamps use `timebase.now_ns()` (monotonic-aligned). Never `datetime.now()`.
- Broker callbacks run in a **separate thread**. Must use `call_soon_threadsafe()` to cross into event loop.
