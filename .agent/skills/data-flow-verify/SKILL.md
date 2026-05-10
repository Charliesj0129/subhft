---
name: data-flow-verify
description: Verify the HFT platform data flow pipeline end-to-end, covering hot path (feed to strategy) and recording path (recorder to ClickHouse/WAL).
---

# Data Flow Verification

## When to Use

- After changes to feed adapter, normalizer, LOB engine, or recorder
- After strategy or risk engine modifications
- Debugging missing data or stale metrics
- Post-deployment smoke test

## Hot Path Verification

The hot path flows: Exchange -> BrokerFacade -> Normalizer -> LOBEngine -> FeatureEngine -> RingBufferBus -> StrategyRunner -> RiskEngine -> OrderAdapter -> BrokerFacade

### Step 1: Services Running

```bash
docker compose ps
# All services should show Up (healthy)
```

### Step 2: Prometheus Metrics Flowing

```bash
curl -s http://localhost:9090/metrics | grep hft_ | head -20
```

If no `hft_` metrics appear, the engine is not running or not exporting.

### Step 3: Feed Events

```bash
# Check feed event counters
curl -s http://localhost:9090/metrics | grep feed_events_total
curl -s http://localhost:9090/metrics | grep raw_queue_depth
```

- `feed_events_total` should be increasing
- `raw_queue_depth` should be low (< 100); high values indicate backpressure

### Step 4: Normalizer Latency

```bash
curl -s http://localhost:9090/metrics | grep normalize_latency_ns
```

- Rust path: expect < 5us (5000 ns)
- Python path: expect < 50us (50000 ns)

### Step 5: LOB Processing

```bash
curl -s http://localhost:9090/metrics | grep lob_process_latency_ns
```

- Rust path: expect < 10us
- Python path: expect < 100us

### Step 6: Strategy Execution

```bash
curl -s http://localhost:9090/metrics | grep -E "strategy_latency_ns|strategy_intents_total"
```

- `strategy_latency_ns` should be < 100us
- `strategy_intents_total` confirms intents are being generated

## Recording Path Verification

The recording path flows: MarketDataService -> recorder_queue -> RecorderService -> Batcher -> DataWriter -> ClickHouse/WAL

### Step 1: ClickHouse Data Count

```bash
docker exec clickhouse clickhouse-client \
  -q "SELECT count() FROM hft.market_data WHERE toDate(exch_ts/1e9) = today()"
```

Should be > 0 during active market hours.

### Step 2: WAL Files

```bash
ls -1 .wal/*.wal 2>/dev/null | wc -l
```

WAL files appear when `HFT_RECORDER_MODE=wal_first` or as fallback on ClickHouse failure.

### Step 3: Recorder Queue Health

```bash
curl -s http://localhost:9090/metrics | grep -E "recorder_queue_depth|recorder_drops_total"
```

- `recorder_queue_depth` should be low
- `recorder_drops_total` should be 0; non-zero means the recorder cannot keep up

## Invariants

These invariants must hold across the entire pipeline:

| Invariant | Rule | Violation Check |
|-----------|------|-----------------|
| Bounded queues | All queues use bounded size with `put_nowait` drop policy | Grep for unbounded `asyncio.Queue()` (no maxsize) |
| Timestamps | All use `timebase.now_ns()`, never `datetime.now()` | `grep -r "datetime.now()" src/hft_platform/` should return 0 hits in hot path |
| Price scaling | All prices are scaled int x10000, never float | `grep -rn "float.*price" src/hft_platform/risk/ src/hft_platform/order/` |
| Non-blocking recording | Recording never blocks hot path | Recorder uses `put_nowait()` with drop on full |

## Troubleshooting

| Symptom | Likely Cause | Diagnostic |
|---------|-------------|------------|
| No feed_events_total | Broker not connected | Check `docker compose logs hft-engine \| grep -i login` |
| High raw_queue_depth | Normalizer too slow or stuck | Check `normalize_latency_ns`, look for blocking calls |
| No ClickHouse rows | Recorder not flushing or CH down | Check `recorder_drops_total`, `docker exec clickhouse clickhouse-client -q "SELECT 1"` |
| WAL files growing | ClickHouse write failures | Check `docker compose logs hft-engine \| grep -i clickhouse` |
| strategy_intents_total = 0 | No signals or strategy disabled | Verify `HFT_MODE`, check strategy logs |
| Queue drops increasing | Backpressure in pipeline | Identify slowest stage via latency metrics |
