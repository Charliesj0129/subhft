# Research Data Access Guide

## Data Source

All L2 market data is stored in the local ClickHouse instance.

| Item                | Value                                          |
| ------------------- | ---------------------------------------------- |
| **Table**           | `hft.market_data`                              |
| **Engine**          | MergeTree                                      |
| **Host**            | `localhost:8123` (HTTP) / `localhost:9000` (native) |
| **Auth**            | user=`default`, password from `.env` `CLICKHOUSE_PASSWORD` |
| **Docker access**   | `docker exec clickhouse clickhouse-client`     |
| **Price convention**| `price_scaled` = Int64 x1,000,000 (divide by 1e6 for float) |
| **Timestamp**       | `exch_ts` / `ingest_ts` = Int64 nanoseconds    |
| **Depth**           | 5-level bid/ask arrays (`bids_price`, `asks_price`, `bids_vol`, `asks_vol`) |
| **Event types**     | `BidAsk` (L2 quotes, ~87%) and `Tick` (trades, ~13%) |

## Best Research Interval

**Recommended**: `2026-03-02` ~ `2026-03-24` (17 consecutive trading days, fully complete)

This interval has:
- 100% trading day coverage (zero missing days)
- Full day session (08:30-13:45) + night session (15:00-05:00)
- 98-120 symbols per day (futures + options + stocks)
- Zero intraday gaps > 5s for futures
- ~8M rows/day average

### Full Data Availability

| Period          | Trading Days | Status      | Notes                                |
| --------------- | ------------ | ----------- | ------------------------------------ |
| 01/26-01/31     | 5            | Complete    | Pre-CNY night sessions only          |
| 02/03-02/06     | 4            | Complete    |                                      |
| **02/07-02/22** | **~10**      | **Sparse**  | Only 02/10, 02/11 have partial data  |
| 02/23-02/26     | 4            | Complete    | Missing 02/27                        |
| **03/02-03/24** | **17**       | **Complete**| Best interval for research           |

### Unusable Dates (< 1000 rows)

- `20260125` (1 row), `20260207` (44 rows), `20260209` (150 rows)

## How to Export Data for Research

### CLI Tool: `ch_batch_export.py`

```bash
# L1 format (.npy) — for alpha feature precompute
python research/tools/ch_batch_export.py \
    --symbols TXFD6,MXFD6,2330 \
    --password "$CLICKHOUSE_PASSWORD" \
    --formats l1 \
    --date-from 2026-03-02 --date-to 2026-03-24

# L2 format (.npz) — for hftbacktest MM backtesting (requires hftbacktest package)
python research/tools/ch_batch_export.py \
    --symbols TXFD6 \
    --password "$CLICKHOUSE_PASSWORD" \
    --formats l1,l2 \
    --date-from 2026-03-02 --date-to 2026-03-24

# Dry-run: check available dates without exporting
python research/tools/ch_batch_export.py \
    --symbols TXFD6 --password "$CLICKHOUSE_PASSWORD" --dry-run

# Concatenate per-day files into single multi-day .npy
python research/tools/ch_batch_export.py \
    --symbols TXFD6 --password "$CLICKHOUSE_PASSWORD" \
    --formats l1 --concat
```

Output goes to `research/data/raw/<symbol>/`.

### Direct ClickHouse Query (Python)

```python
import clickhouse_connect

client = clickhouse_connect.get_client(
    host="localhost", port=8123,
    username="default", password="<from .env CLICKHOUSE_PASSWORD>",
)

# Discover available dates for a symbol
dates = client.query("""
    SELECT toDate(fromUnixTimestamp64Nano(ingest_ts)) AS dt, count() AS rows
    FROM hft.market_data
    WHERE symbol = '2330'
    GROUP BY dt HAVING rows > 100
    ORDER BY dt
    SETTINGS max_memory_usage=2500000000
""").result_rows

# Fetch one day of L2 data
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

### Direct ClickHouse Query (CLI)

```bash
docker exec clickhouse clickhouse-client --query "
  SELECT symbol, count(), min(toDateTime(exch_ts/1e9, 'Asia/Taipei')), max(toDateTime(exch_ts/1e9, 'Asia/Taipei'))
  FROM hft.market_data
  WHERE toDate(fromUnixTimestamp64Nano(ingest_ts)) = '2026-03-05'
  GROUP BY symbol ORDER BY count() DESC LIMIT 20
  SETTINGS max_memory_usage=2500000000
"
```

## Output Formats

### L1 Research (.npy)

Structured numpy array for alpha feature precompute:

| Field        | Type  | Description           |
| ------------ | ----- | --------------------- |
| `bid_px`     | f64   | Best bid price (float)|
| `ask_px`     | f64   | Best ask price (float)|
| `bid_qty`    | f64   | Best bid quantity      |
| `ask_qty`    | f64   | Best ask quantity      |
| `mid_price`  | f64   | (bid + ask) / 2       |
| `spread_bps` | f64   | Spread in basis points |
| `volume`     | f64   | Trade volume           |
| `local_ts`   | i64   | Ingest timestamp (ns)  |

### L2 HftBacktest (.npz)

Event-based format compatible with `hftbacktest` package:
- `DEPTH_SNAPSHOT_EVENT` — initial order book state
- `DEPTH_EVENT` — incremental L2 updates (5 levels bid + ask)
- `TRADE_EVENT` — executed trades
- Built-in de-dup: identical BidAsk within 0.5ms window are filtered

### Metadata Sidecar (.meta.json)

Every exported file has a `.meta.json` sidecar with:
`dataset_id`, `source_type`, `source`, `rows`, `fields`, `symbols`, `date`, `data_fingerprint`, `data_ul`

## Asset Coverage

### Futures (15 symbols)

| Symbol | Name        | Note           |
| ------ | ----------- | -------------- |
| TXFD6  | 台指期 04   | Most liquid    |
| MXFD6  | 小台指 04   | High frequency |
| TMFD6  | 微台指 04   | Highest tick count |
| TXFE6  | 台指期 05   | Far month      |
| MXFE6  | 小台指 05   | Far month      |

### Stocks (71 symbols)

Top: 2408, 2303, 2409, 1303, 2317, 1326, 2330, 2327, 1301, 2609 ...

### Options (160 symbols)

TXO call/put across multiple strikes and months.

## Data Quality Notes

1. **Deduplication**: MergeTree does not auto-deduplicate. `ch_batch_export.py` L2 export has built-in dedup (0.5ms window). Raw table has < 0.01% duplicates on most days; 20260310 has ~0.55% due to migration overlap.
2. **Price scaling**: `price_scaled` uses x1,000,000 in ClickHouse (note: different from live platform's x10,000 convention). Always verify with `CH_PRICE_SCALE_INT = 1_000_000.0` in `ch_batch_export.py`.
3. **Night session**: Futures night session (15:00-05:00) data is partitioned by `ingest_ts`, not `exch_ts`. A single partition may span two calendar dates.
4. **TTL**: Table has 6-month TTL on `ingest_ts`. Data older than 6 months is auto-deleted.
5. **Memory limit**: ClickHouse has a 2GB per-query memory limit. For large queries, add `SETTINGS max_memory_usage=2500000000` or filter by date/symbol.
