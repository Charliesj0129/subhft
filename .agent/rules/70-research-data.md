# Research Data Access Guide

## Data Source

All L2 market data is in local ClickHouse.

| Item                | Value                                          |
| ------------------- | ---------------------------------------------- |
| **Table**           | `hft.market_data` (MergeTree)                  |
| **Host**            | `localhost:8123` (HTTP) / `localhost:9000` (native) |
| **Auth**            | user=`default`, password from `.env` `CLICKHOUSE_PASSWORD` |
| **Docker access**   | `docker exec clickhouse clickhouse-client`     |
| **Price**           | `price_scaled` Int64 x1,000,000 (note: differs from platform's x10,000) |
| **Timestamp**       | `exch_ts` / `ingest_ts` Int64 nanoseconds      |
| **Depth**           | 5-level arrays: `bids_price`, `asks_price`, `bids_vol`, `asks_vol` |
| **Event types**     | `BidAsk` (L2 quotes, ~87%) and `Tick` (trades, ~13%) |
| **TTL**             | 6 months on `ingest_ts`                        |
| **Memory limit**    | 2GB/query; add `SETTINGS max_memory_usage=2500000000` for large queries |

## Best Research Interval

**Recommended**: `2026-03-02` to `2026-03-24` (17 consecutive complete trading days, ~8M rows/day, 98-120 symbols).

### Data Availability

| Period          | Trading Days | Status      |
| --------------- | ------------ | ----------- |
| 01/26-01/31     | 5            | Complete (pre-CNY night only) |
| 02/03-02/06     | 4            | Complete    |
| 02/07-02/22     | ~10          | **Sparse** (only 02/10, 02/11 partial) |
| 02/23-02/26     | 4            | Complete (02/27 missing) |
| **03/02-03/24** | **17**       | **Complete — best interval** |

Unusable dates (< 1000 rows): `20260125`, `20260207`, `20260209`.

## Export CLI: `research/tools/ch_batch_export.py`

```bash
# L1 (.npy) for alpha feature precompute
python research/tools/ch_batch_export.py \
    --symbols TXFD6,MXFD6,2330 \
    --password "$CLICKHOUSE_PASSWORD" \
    --formats l1 \
    --date-from 2026-03-02 --date-to 2026-03-24

# L2 (.npz) for hftbacktest MM backtesting
python research/tools/ch_batch_export.py \
    --symbols TXFD6 --password "$CLICKHOUSE_PASSWORD" \
    --formats l1,l2 --date-from 2026-03-02 --date-to 2026-03-24

# Dry-run / concat flags also supported: --dry-run, --concat
```

Output: `research/data/raw/<symbol>/`.

## Direct ClickHouse Query (Python)

```python
import clickhouse_connect
client = clickhouse_connect.get_client(
    host="localhost", port=8123,
    username="default", password="<from .env CLICKHOUSE_PASSWORD>",
)
rows = client.query("""
    SELECT exch_ts, type, price_scaled, volume,
           bids_price, bids_vol, asks_price, asks_vol
    FROM hft.market_data
    WHERE symbol = 'TXFD6'
      AND toDate(fromUnixTimestamp64Nano(ingest_ts)) = '2026-03-05'
    ORDER BY exch_ts, seq_no
    SETTINGS max_memory_usage=2500000000
""").result_rows
```

## Output Formats

### L1 (.npy) — structured numpy array

Fields: `bid_px`, `ask_px`, `bid_qty`, `ask_qty`, `mid_price`, `spread_bps`, `volume` (all f64); `local_ts` (i64 ns).

### L2 (.npz) — hftbacktest-compatible

Events: `DEPTH_SNAPSHOT_EVENT`, `DEPTH_EVENT` (5 levels), `TRADE_EVENT`. Built-in dedup: identical BidAsk within 0.5ms filtered.

### Metadata Sidecar (.meta.json)

Every export has a sidecar with: `dataset_id`, `source_type`, `source`, `rows`, `fields`, `symbols`, `date`, `data_fingerprint`, `data_ul`.

## Asset Coverage

- **Futures (15)**: `TXFD6` (most liquid), `MXFD6` (HF), `TMFD6` (highest tick count), `TXFE6`/`MXFE6` (far month), …
- **Stocks (71)**: Top 10: 2408, 2303, 2409, 1303, 2317, 1326, 2330, 2327, 1301, 2609.
- **Options (160)**: TXO call/put across multiple strikes/months.

## Data Quality Notes

1. **Dedup**: MergeTree does not auto-dedupe. `ch_batch_export.py` L2 dedups within 0.5ms. Raw duplicates < 0.01% most days; 20260310 ~0.55% (migration overlap).
2. **Price scale mismatch**: ClickHouse x1,000,000; live platform x10,000. Verify via `CH_PRICE_SCALE_INT = 1_000_000.0` in `ch_batch_export.py`.
3. **Night session**: Futures 15:00-05:00 partitioned by `ingest_ts`, not `exch_ts` — one partition may span two calendar dates.
