# R47 Revalidation Round — Data Inventory Verification (2026-04-24)

**Status**: VERIFIED. Blocker for T5 (≥60d survivor rule). Lead decision pending.
**Round**: `alpha-research-20260424-r47-revalidation` (task #2, T5-prep)
**Scope**: Read-only verification of ≥60d TMFD6/TXFD6/TMFE6 availability across local + remote ClickHouse before any T5 backtest runs.
**Companion machine-readable artifact**: `outputs/team_artifacts/alpha-research/round-1/artifacts/data_inventory_verified.json`

## TL;DR

The shared-context.yaml `remote_days_claim: 58 (2026-01-27..2026-03-26)` is **wrong**. Remote `hft.market_data` is the *recent live* archive (2026-04-07..04-24), not the historical archive. Local CK is the historical archive (2026-01-26..04-17). The two are **almost-disjoint** windows, and even their union is **≤31 days for D6 contracts and 25 days for TMFE6** — well short of the 60d survivor floor.

| Instrument | Local days | Remote days | Union days | ≥60d? |
|------------|-----------:|------------:|-----------:|-------|
| TMFD6      | **31**     | 7           | **31**     | NO    |
| TXFD6      | **31**     | 7           | **31**     | NO    |
| TMFE6      | 20         | 8           | **25**     | NO    |

No path to 60d exists in currently queryable data.

## Method

### Tunnel (no credentials in command line)

```bash
# Env loaded from .env.remote.local (REMOTE_USER, REMOTE_IP); auth = SSH key
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
    -L 19000:127.0.0.1:9000 \
    -L 18123:127.0.0.1:8123 \
    "${REMOTE_USER}@${REMOTE_IP}" &
```

- 19000 → remote CK native (TCP/9000)
- 18123 → remote CK HTTP (8123)
- Local CK on default 8123/9000 — no port collision.
- Tunnel closed at end of task; both ports verified not listening.

### Identity verification

| Side   | Version    | Hostname (container ID) |
|--------|------------|-------------------------|
| Local  | 25.12.3.21 | 36ab4903e662            |
| Remote | 25.12.3.21 | c4de59c0ea6d            |

Two distinct CK instances — the tunnel reaches a separate host, not loopback.

### Queries (read-only)

```sql
-- per-instrument inventory
SELECT count(), countDistinct(toDate(toDateTime64(exch_ts/1e9,3))),
       min(toDate(toDateTime64(exch_ts/1e9,3))), max(toDate(toDateTime64(exch_ts/1e9,3))),
       max(length(bids_price))
FROM hft.market_data WHERE symbol = '<SYM>';

-- daily row counts (gap analysis)
SELECT toDate(toDateTime64(exch_ts/1e9,3)) AS d, count()
FROM hft.market_data WHERE symbol='<SYM>' GROUP BY d ORDER BY d;

-- whole-table partition scope
SELECT partition, sum(rows), formatReadableSize(sum(bytes_on_disk))
FROM system.parts WHERE database='hft' AND table='market_data' AND active
GROUP BY partition ORDER BY partition;
```

No INSERT / ALTER / DELETE / DDL.

## Per-instrument results

### TMFD6

| Source | Rows       | Distinct days | Range                  | Max L |
|--------|-----------:|--------------:|------------------------|------:|
| Local  | 17,510,333 | **31**        | 2026-01-26 .. 04-15    | 5     |
| Remote |  4,006,532 | **7**         | 2026-04-07 .. 04-15    | 5     |
| Union  |          — | **31**        | 2026-01-26 .. 04-15    | 5     |

Remote is a strict subset of local. Remote-only days: `[]`. The 7 remote days (04-07..04-15) are already present locally with identical row counts.

Local TMFD6 daily rows (observed):
```
2026-01-26..30, 01-31, 02-03..06, 02-23..25,
03-19,20, 23,24, 26, 27, 30,31,
04-01,02, 04-03 (52,626 rows — partial day),
04-07..10, 04-13..15
```

Calendar gaps in local panel:
- 2026-02-07..02-22 (~12 trading days) missing
- 2026-02-27..03-18 (~14 trading days) missing
- 2026-03-21,22,25,28,29 (weekends + 1 trading-day)
- 2026-04-04..06 (weekend + 1 trading-day)
- 2026-04-03 partial → treat as suspect → clean panel ≈ 30 days

### TXFD6

| Source | Rows       | Distinct days | Range                  | Max L |
|--------|-----------:|--------------:|------------------------|------:|
| Local  | 11,176,722 | **31**        | 2026-01-26 .. 04-15    | 5     |
| Remote |  2,501,394 | **7**         | 2026-04-07 .. 04-15    | 5     |
| Union  |          — | **31**        | 2026-01-26 .. 04-15    | 5     |

Same shape as TMFD6 — remote is a strict subset, adds no new days.

### TMFE6 (newly rolled front contract)

| Source | Rows       | Distinct days | Range                  | Max L |
|--------|-----------:|--------------:|------------------------|------:|
| Local  |  7,300,161 | **20**        | 2026-02-25 .. 04-17    | 5     |
| Remote |  5,468,211 | **8**         | 2026-04-15 .. 04-24    | 5     |
| Union  |          — | **25**        | 2026-02-25 .. 04-24    | 5     |

Remote-only days: `2026-04-20, 04-21, 04-22, 04-23, 04-24` — 5 days not present locally. Noteworthy: **04-21 is the R47 live-incident day** (`2026-04-21-r47-backtest-live-divergence.md`). Intersection: 04-15, 04-16, 04-17 (3 days).

## Whole-table scope (sanity)

### Remote `hft.market_data`
- 65.5M rows, 384 symbols, 2026-04-07..04-24 (14 partition days).
- Engine MergeTree, partition `toYYYYMMDD(toDateTime(ingest_ts/1e9))`.
- Live/recent archive, not historical.

### Local `hft.market_data`
- 45 partition days, 2026-01-26..04-17.
- Historical archive used by `research.backtest.maker_engine.ClickHouseSource`.

Two CK instances cover **different windows** with a 7-day overlap (04-07..04-15 for D6; 04-15..04-17 for TMFE6).

## Verdict

**≥60d on remote ClickHouse: NO** for every R47-relevant instrument. The shared-context 58-day remote claim is contradicted by direct measurement. The historical archive lives only on the local box.

| Instrument | Best achievable | Survivor floor | Gap |
|------------|----------------:|---------------:|----:|
| TMFD6      | 31 (local ∪ remote = local) | 60 | -29 |
| TXFD6      | 31 (local ∪ remote = local) | 60 | -29 |
| TMFE6      | 25 (union)      | 60             | -35 |

## Recommended action (Lead decision)

1. **[Preferred]** Accept the 31d/25d sample with mandatory jackknife + `max_day_pnl / total_pnl ≤ 25%` per scorecard, and a `sample_warning: small_sample_31d_panel` field baked in. Preserves the spirit of the single-day-dominance check on data we actually have. Note: this rule should automatically kill the 2026-04-24 audit's spread=7 +3,302 result (1 winning day in 31).
2. Time-extend local CK via broker-WAL replay or external backfill — out of round scope (ops work, not addressable inside a 24h research loop).
3. Skip T5 entirely; exit the round with a "no T5 evidence" verdict pointing at the data-availability gap. Cleanest from a research-integrity standpoint, but produces no positive output.

Executor recommendation: **Option 1** with jackknife + max-day-pnl ratio + sample_warning mandatory on every scorecard. Lead may additionally authorize a TMFE6 5-day remote-only pass (04-20..04-24, including the 04-21 incident day) as a separate "incident-day replay" for any V3 hedge candidate.

## shared-context.yaml corrections Lead should apply

- `data_inventory.TMFD6.remote_days_claim`: **58 → 7**
- `data_inventory.TMFD6.remote_range_claim`: **"2026-01-27 ~ 2026-03-26" → "2026-04-07 ~ 2026-04-15"**
- `data_inventory.TXFD6.remote_days_claim`: **58 → 7** (range same as above)
- `data_inventory.TMFE6.remote_days_claim`: **partial → 8** with range `2026-04-15 ~ 2026-04-24`
- `data_inventory.TMFD6.ge_60d_available`: **unknown_requires_verification → false** (and analogous for TXFD6/TMFE6)

## Out of scope per task brief

- Did NOT modify `shared-context.yaml`.
- Did NOT run any backtest.
- Did NOT write to remote CK.
- Tunnel torn down at end of task; `/tmp/ck_tunnel.pid` removed.
