# Local ClickHouse `hft.market_data` corpus — provenance and TTL divergence

Status: current as of 2026-08-06. Applies to the **local/research** ClickHouse
container only, not the production host.

Coverage numbers below are **generated**, not hand-maintained: run
`make research-data-quality DATE_FROM=2026-01-26 DATE_TO=<today>` and read
`research/reports/data_quality/*_source_audit.{json,md}`
(`research/data_pipeline/quality.py`, see `docs/modules/data_quality.md`).

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
| Range (Asia/Taipei) | 2026-01-26 → 2026-08-05 |
| Rows | 936,216,159 |
| On disk | 22.12 GiB, 182 active parts |
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

Measured 2026-08-06 by `make research-data-quality` over the whole corpus
(936,216,159 rows, report `9606665855875e86…`). Of the **126 XTAI sessions**
between 2026-01-26 and 2026-08-05:

| Status | Sessions | Meaning |
|---|---|---|
| clean | 90 | rows and symbols at the local baseline |
| partial | 12 | present but below baseline |
| degraded | 8 | symbol count collapsed, or <10% of baseline rows |
| **missing** | **16** | exchange session with **zero** rows |

(A further 22 calendar dates hold rows but are not sessions — they are the
post-midnight tail of the previous night session, which lands on the next calendar
date. They are labelled `non_session` and excluded from the tally.)

**Missing — 16 sessions**: `02-02`, `02-09`, `02-10`, `02-11`, `03-02`, then the
11-day block `06-25`, `06-26`, `06-29`, `06-30`, `07-01`, `07-02`, `07-03`, `07-06`,
`07-13`, `07-14`, `07-16`. The `07-13 → 07-19` part corresponds to the shioaji 1.5.6
deploy / connectivity incident; the production engine has run clean since
2026-07-19T12:56Z.

**Degraded — 8 sessions**:

| Date | Rows | Symbols |
|---|---|---|
| 2026-01-26 | 565,101 | 78 |
| 2026-03-25 | 229,017 | 48 |
| 2026-04-01 | 3,552,023 | 48 |
| 2026-04-23 | 974,246 | **1** |
| 2026-04-24 | 1,059,883 | 6 |
| 2026-07-10 | 577,597 | 57 |
| 2026-07-15 | 506,454 | 164 |
| 2026-08-05 | 676,269 | 246 |

**Partial — 12 sessions**: `02-03`, `02-24`, `03-19`, `03-20`, `04-16`, `05-04`,
`05-07`, `05-11`, `05-21`, `05-22`, `06-15`, `06-18`.

**Longest unbroken clean runs** — use these, not a hand-picked window:

| Sessions | Range |
|---|---|
| 15 | 2026-05-25 → 2026-06-12 |
| 13 | 2026-07-17 → 2026-08-04 |
| 12 | 2026-03-03 → 2026-03-18 |
| 8 | 2026-04-02 → 2026-04-15 |
| 7 | 2026-05-12 → 2026-05-20 |

> **Correction.** `.agent/rules/70-research-data.md` used to call
> `2026-03-02 → 2026-03-24` the "best known complete research interval". It is not:
> **`2026-03-02` has zero rows**, and `03-19`/`03-20` are partial while `03-25` is
> degraded. The real clean run in that month is `2026-03-03 → 2026-03-18`.

**Symbol universe is not constant.** It ranges 1–523 across the corpus, with 29
day-over-day steps clearing both a 5-symbol and a 15% floor. Cross-period studies
must not assume a fixed universe. Note the audit cannot see *small* pool changes
(the documented 368 → 357 is ~3%, the same magnitude as monthly contract rollover
churn) — see `docs/modules/data_quality.md`.

## Other findings from the 2026-08-06 audit

- **`exch_ts` causality is clean table-wide**: 0 rows with `exch_ts > ingest_ts + 1s`
  across all 936 M rows, `max(exch_ts - ingest_ts)` = 46 ms. The 2026-08-05 repair of
  the +8h shift holds.
- **`trade_direction` population is a hard family constraint** (Tick rows only):
  `202601`–`202603` = **0.0**, `202604` = 0.928, `202605` = 0.998, `202606` = 0.997,
  `202607` = 0.999, `202608` = 1.000. Aggressor-split research cannot use Q1 at all.
- **5,593 duplicate `(symbol, exch_ts, ingest_ts, seq_no)` rows** on 5 days:
  `02-26` (426), `03-03` (7), `03-31` (144), `04-27` (1,387), `04-28` (3,629).
  Consistent with partial re-inserts — `market_data` is a plain MergeTree, so a
  re-import duplicates rather than replaces (see the 2026-07-25 pull above).
- **3,189,310 `BidAsk` rows carry empty depth arrays** (0.34% of the corpus).
- **`2026-04-03` is the one day with rows outside a session window** (17.3%,
  198,678 rows). It is a market holiday; hours 06:00–12:59 Taipei carry ~187 k events
  from only 5–6 symbols. Worth identifying which instruments those are — a feed that
  does not observe the TW holiday calendar would explain it.

## Related

- `.agent/rules/70-research-data.md` — price scale and export contract
- `docs/operations/data-retention-policy.md` — retention intent
- `src/hft_platform/migrations/clickhouse/` — schema source of truth (still
  declares the TTL this table no longer has)
