# Local ClickHouse `hft.market_data` corpus — provenance and TTL divergence

Status: current as of 2026-07-25. Applies to the **local/research** ClickHouse
container only, not the production host.

## Why this file exists

Two things happened on 2026-07-25 that a future reader must not have to
re-derive:

1. The local corpus was extended with production data pulled off THESHOW
   (`charl-AB350M-Gaming-3`), so the local table now spans 2026-01-26 → 2026-07-25.
2. The local table's 6-month TTL was **removed**, because it was hours away from
   starting to delete the oldest month. Local ClickHouse is the only remaining
   regeneration source for research data (the L2 NPZ corpus was deleted on
   2026-07-20), so the TTL was actively destructive here.

## Current contents

| Property | Value |
|---|---|
| Range (Asia/Taipei) | 2026-01-26 → 2026-07-25 |
| Rows | 877,326,445 |
| On disk | 20.56 GiB, 158 active parts |
| Engine | `MergeTree`, `PARTITION BY toYYYYMMDD(toDateTime(ingest_ts/1000000000))` (**UTC**), `ORDER BY (symbol, exch_ts, ingest_ts)` |
| TTL | **none** (see below) |
| Price scale | **×1,000,000** raw — the live platform scale is ×10,000; conversions must be explicit (`.agent/rules/70-research-data.md`) |

Note the partition key is UTC while trading days are Asia/Taipei (UTC+8), so one
Taipei session's early hours land in the previous UTC partition. Always group by
`toDate(fromUnixTimestamp64Nano(ingest_ts),'Asia/Taipei')` for trading-day
analysis, and by `toYYYYMMDD(toDateTime(ingest_ts/1000000000))` only for
partition operations.

## TTL removal (2026-07-25)

The table carried the production DDL's
`TTL toDateTime(ingest_ts/1000000000) + toIntervalMonth(6)`. `system.parts`
showed partition `20260126` (1,087,967 rows) expiring at **2026-07-26 13:18:45**,
then roughly one trading day rolling off per day, with
`ttl_only_drop_parts = 0` and `merge_with_ttl_timeout = 14400` — i.e. background
merges would have silently rewritten parts and dropped expired rows.

Applied:

```sql
ALTER TABLE hft.market_data REMOVE TTL   -- metadata-only, no part rewrite
```

Consequences to remember:

- **The local table now diverges from the migration DDL** in
  `src/hft_platform/migrations/clickhouse/`. Recreating the table (fresh volume,
  re-running the create migration) reinstates the 6-month TTL and will start
  deleting history again. Re-apply `REMOVE TTL` after any such recreate.
- `system.parts.delete_ttl_info_min` still shows the old per-part expiry
  timestamps; that is stale leftover metadata, not an active policy.
- **Production TTL was deliberately left in place.** THESHOW's 216 G volume needs
  the 6-month bound. Do not "fix" it there.
- Only `hft.market_data` was affected locally. Twelve other `hft` tables carry
  TTLs but hold 0 rows; `orders`, `fills_legacy_pre_rmt` and `config_snapshots`
  hold a few rows that do not expire until 2027.

## The 2026-07-25 pull from THESHOW

Source: THESHOW production ClickHouse, both sides on 25.12.3.21. Read-only on the
remote; nothing was written to the production host. Script:
`scratchpad/pull_theshow_market_data.sh` (per-partition `FORMAT Native` over SSH
with `pigz`, verified by `count()` + `sum(cityHash64(symbol, exch_ts, ingest_ts,
price_scaled, volume, seq_no))` on both sides before and after each partition).

- **Transferred: 19 partitions / 95,461,225 rows**, all hash-verified.
- **Skipped: 6 partitions** (`20260605`, `20260608`–`20260612`, 47,080,325 rows)
  that were already local with identical count *and* hash. `market_data` is a
  plain MergeTree, so re-inserting would have duplicated rather than replaced.
- Combined 2026-06-05 → 2026-07-25 total: **142,541,550 rows**, matching the
  remote table exactly.

The two tables order `instrument_type` differently in their physical schema (both
have the same 18 columns), so the transfer named all columns explicitly on both
the `SELECT` and the `INSERT`. Any future Native transfer must do the same.

The pull captured everything the remote held as of 2026-07-25 17:29 CST. The
night session had already closed, so nothing was in flight — but **Monday
2026-07-27 onwards will need another pull**.

## Coverage — real holes, not transfer failures

Daily row counts and symbol counts for the pulled range (Asia/Taipei):

| Date | Rows | Symbols | Note |
|---|---|---|---|
| 06-05 | 6,689,815 | 368 | |
| 06-06 | 420,000 | 81 | partial |
| 06-08 → 06-12 | 7.3 M – 8.3 M/day | 368 | clean week |
| 06-13 | 371,108 | 265 | partial |
| 06-15 | 996,044 | 318 | degraded |
| 06-16 | 6,381,148 | 368 | |
| 06-17 | 4,473,069 | 368 | |
| 06-18 | 3,696,596 | 259 | degraded |
| 06-19 | 35,585 | 209 | degraded |
| 06-22 | 5,839,609 | 342 | |
| 06-23 | 8,136,199 | 357 | |
| 06-24 | 7,273,301 | 357 | |
| 07-07 | 2,826,569 | 357 | |
| 07-08 | 8,352,916 | 357 | |
| 07-09 | 7,789,089 | 357 | |
| 07-10 | 577,597 | 57 | degraded |
| 07-15 | 506,454 | 164 | degraded |
| 07-17 | 4,318,658 | 351 | |
| 07-18 | 470,233 | 201 | partial |
| 07-20 → 07-24 | 6.0 M – 7.2 M/day | 296 | clean week |
| 07-25 | 682,795 | 246 | partial (night session only) |

**Missing entirely — 11 trading days**: 06-25, 06-26, 06-29, 06-30, 07-01, 07-02,
07-03, 07-06, 07-13, 07-14, 07-16. The 07-13 → 07-19 gap corresponds to the
shioaji 1.5.6 deploy / connectivity incident; the production engine has run clean
since 2026-07-19T12:56Z.

**Symbol universe stepped 368 → 357 → 296**, the last change from 2026-07-20
(pool config change). Cross-period studies must not assume a constant universe.

## Related

- `.agent/rules/70-research-data.md` — price scale and export contract
- `docs/operations/data-retention-policy.md` — retention intent
- `src/hft_platform/migrations/clickhouse/` — schema source of truth (still
  declares the TTL this table no longer has)
