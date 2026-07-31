# Local ClickHouse `hft.market_data` corpus — provenance and TTL divergence

Status: current as of 2026-08-06. Applies to the **local/research** ClickHouse
container only, not the production host.

Coverage numbers below are **generated**, not hand-maintained: run
`make research-data-quality DATE_FROM=2026-01-26 DATE_TO=<today>` and read
`research/reports/data_quality/*_source_audit.{json,md}`
(`research/data_pipeline/quality.py`, see `docs/modules/data_quality.md`).

## Why this file exists

Two corpus changes that a future reader must not have to re-derive:

1. The local corpus was extended with production data pulled off THESHOW
   (`charl-AB350M-Gaming-3`), and later received a verified closed-partition
   repair through UTC partition 20260729.
2. The local table's 6-month TTL was **removed**, because it was hours away from
   starting to delete the oldest month. Local ClickHouse is the only remaining
   regeneration source for research data (the L2 NPZ corpus was deleted on
   2026-07-20), so the TTL was actively destructive here.

## Current contents

| Property | Value |
|---|---|
| Range (Asia/Taipei) | 2026-01-26 → 2026-08-06 |
| Rows | 949,820,979 |
| On disk | 22.49 GiB, 186 active parts, 113 partitions |
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

## Keeping the archive alive — `scripts/sync_market_data_archive.py`

This local table is the **only durable copy** of research market data: its TTL is gone
(below) and the L2 NPZ corpus was deleted on 2026-07-20. Upstream still enforces the
DDL's 6-month TTL, so anything not pulled before a partition expires is lost for good.

```bash
make research-archive-sync                       # dry-run diff + upstream inventory
uv run python scripts/sync_market_data_archive.py --partitions 20260805,20260806 --sync
```

The upstream host comes from `HFT_ARCHIVE_REMOTE` (environment or `.env`) or `--remote`.
Dry-run is the default; writing requires `--sync`.

Properties that matter, and why:

- **Read-only upstream.** Every remote statement is a `SELECT`.
- **Refuses a partition that already holds local rows.** `market_data` is a plain
  `MergeTree`, so re-inserting *duplicates* rather than replaces. This is exactly why
  the 2-row shortfall on partition `20260804` cannot be repaired by re-pulling it —
  that would duplicate 7.3M rows. Those 2 rows stay missing.
- **Skips the in-flight partition** (today, UTC): a partition still being written
  cannot be hash-verified.
- **Verifies both sides** with `count()` + `sum(cityHash64(symbol, exch_ts, ingest_ts,
  price_scaled, volume, seq_no))` before and after each transfer. On mismatch it aborts
  and prints the `DROP PARTITION` remediation rather than dropping anything itself.
- **Column order is read from `system.columns` at runtime** and named explicitly on
  both the `SELECT` and the `INSERT`, because the two tables order `instrument_type`
  differently in their physical schema.

> **Why this is a script and not a runbook paragraph.** Until 2026-08-07 this procedure
> lived in `scratchpad/pull_theshow_market_data.sh`, which this document cited as if it
> were an asset. It was never committed and did not survive its session — the directory
> no longer exists. The archive's only maintenance tool was therefore unrecoverable
> prose. `archive_sync` in the source auditor now measures the gap so that losing the
> habit is *detectable* rather than discovered months later.

### The 2026-07-25 pull from THESHOW

Source: THESHOW production ClickHouse, both sides on 25.12.3.21. Read-only on the
remote; nothing was written to the production host. The unretained ad-hoc
predecessor of the script above transferred per-partition `FORMAT Native` over
SSH with `pigz` and used the same count-and-digest verification on both sides.

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
night session had already closed, so nothing was in flight.

### The 2026-07-31 closed-partition repair

Only UTC ingest partitions that were absent locally and did not overlap the
active local WAL were copied. Remote ClickHouse remained read-only. Data first
landed in a separate local staging table; every partition had to match the
remote count, symbol count, and content hash before it was inserted into
`hft.market_data`.

| UTC ingest partition | Rows | Symbols | Content hash |
|---|---:|---:|---:|
| 20260727 | 7,064,800 | 296 | 11097430841529907375 |
| 20260728 | 8,268,651 | 296 | 16208870874059515396 |
| 20260729 | 10,838,879 | 296 | 9099460484293833097 |

The repair added **26,172,330 rows**. Post-merge verification matched all three
remote fingerprints exactly, after which the staging table was dropped.
Partitions `20260730` and `20260731` were deliberately not copied because they
overlapped an active local WAL and could duplicate rows in this plain
`MergeTree`. The Asia/Taipei wall-date range reaches 2026-07-30 because UTC
partition 20260729 includes early 2026-07-30 local timestamps; that does not
make the 2026-07-30 trading session complete.

A fresh governed preflight over the repaired window (ending 2026-07-29)
accepted 60 eligible bidask/kbar trading days and 43 eligible tick trading
days, up from 58 and 41 in the prior campaign. Trading date 2026-07-27 remained
excluded because the TMF night session was incomplete; only 2026-07-28 and
2026-07-29 added eligible evidence. All contracts observed by the repaired
exports had frozen cost-profile coverage, but every family remains below the
100-day full-run floor.

### The 2026-08-07 sync

First run of the versioned script. Partitions `20260805` (6,905,958 rows) and
`20260806` (6,698,862 rows) transferred and digest-verified identical on both sides;
`20260807` was skipped as in-flight. The archive went **936,216,159 → 949,820,979 rows**
across 111 → 113 partitions.

The pre-sync diff also established the shape of the archive's relationship to upstream,
which is worth keeping:

| | |
|---|---|
| Shared partitions | **byte-identical row counts**, with exactly one exception |
| `20260804` | local 7,338,496 vs upstream 7,338,498 — a **+2 watermark boundary** from the previous pull, permanently unrepairable |
| Upstream extent | oldest active partition `20260605`; upstream is **not** a backstop for anything older |
| `06-25` → `07-06`, `07-13`/`07-14`/`07-16` | **absent upstream too** — a recording outage, not a sync failure, and permanently lost |
| Local-only | 78 partitions (`20260126`–`20260604`) that have already aged out upstream |

Note the upstream TTL is genuinely active and computable per partition
(`system.parts.delete_ttl_info_max`): as of 2026-08-07 the oldest upstream partition
expires 2026-12-05. The ~2-month upstream extent is therefore **not** TTL attrition —
data older than `20260605` was lost upstream for some other reason.

The post-sync audit's `archive_sync` check reports the residual state precisely:
one partition missing locally (`20260807`, 4,385,195 rows and still growing) and one
row-count delta (`20260804`, −2). Severity is `warn`, not `error`, because that
partition's upstream copy has **183 days** of runway — far outside the 30-day urgent
horizon. That is the intended reading: behind, but not yet losing anything.

## Coverage — real holes, not transfer failures

Measured 2026-08-07 by `make research-data-quality` over the whole corpus
(949,078,108 rows in the audited range, report `2ae03ee9352ec7ed…`). Of the
**127 XTAI sessions** between 2026-01-26 and 2026-08-06:

| Status | Sessions | Meaning |
|---|---|---|
| clean | 92 | rows and symbols at the local baseline |
| partial | 12 | present but below baseline |
| degraded | 7 | symbol count collapsed, or <10% of baseline rows |
| **missing** | **16** | exchange session with **zero** rows |

Against the 2026-08-06 baseline (`9606665855875e86…`, 90/12/8/16) the only two
changes are the two days the sync recovered: `2026-08-05` moved `degraded` →
`clean` (676,269 rows / 246 symbols → 6,846,720 / 296) and `2026-08-06` is a new
`clean` session (6,691,498 / 296). **No other day changed status** — verified by
diffing the per-day arrays of the two reports, which matters because the rolling
11-day baseline median shifts when days are added.

(A further 22 calendar dates hold rows but are not sessions — they are the
post-midnight tail of the previous night session, which lands on the next calendar
date. They are labelled `non_session` and excluded from the tally.)

**Missing — 16 sessions**: `02-02`, `02-09`, `02-10`, `02-11`, `03-02`, then the
11-day block `06-25`, `06-26`, `06-29`, `06-30`, `07-01`, `07-02`, `07-03`, `07-06`,
`07-13`, `07-14`, `07-16`. The `07-13 → 07-19` part corresponds to the shioaji 1.5.6
deploy / connectivity incident; the production engine has run clean since
2026-07-19T12:56Z.

**Degraded — 7 sessions**:

| Date | Rows | Symbols |
|---|---|---|
| 2026-01-26 | 565,101 | 78 |
| 2026-03-25 | 229,017 | 48 |
| 2026-04-01 | 3,552,023 | 48 |
| 2026-04-23 | 974,246 | **1** |
| 2026-04-24 | 1,059,883 | 6 |
| 2026-07-10 | 577,597 | 57 |
| 2026-07-15 | 506,454 | 164 |

**Partial — 12 sessions**: `02-03`, `02-24`, `03-19`, `03-20`, `04-16`, `05-04`,
`05-07`, `05-11`, `05-21`, `05-22`, `06-15`, `06-18`.

**Longest unbroken clean runs** — use these, not a hand-picked window:

| Sessions | Range |
|---|---|
| 15 | 2026-05-25 → 2026-06-12 |
| 15 | 2026-07-17 → 2026-08-06 |
| 12 | 2026-03-03 → 2026-03-18 |
| 8 | 2026-04-02 → 2026-04-15 |
| 7 | 2026-05-12 → 2026-05-20 |

The July–August run was 13 sessions (`→ 2026-08-04`) before the 2026-08-07 sync;
recovering `08-05` and `08-06` extended it to 15 and made it the most recent
clean window in the corpus. Runs are counted over consecutive *sessions* —
weekends and holidays are not breaks.

> **Correction.** `.agent/rules/70-research-data.md` used to call
> `2026-03-02 → 2026-03-24` the "best known complete research interval". It is not:
> **`2026-03-02` has zero rows**, and `03-19`/`03-20` are partial while `03-25` is
> degraded. The real clean run in that month is `2026-03-03 → 2026-03-18`.

**Symbol universe is not constant.** It ranges 1–523 across the corpus, with 28
day-over-day steps clearing both a 5-symbol and a 15% floor (29 before the sync —
recovering `08-05`'s full 296-symbol universe removed one step). Cross-period studies
must not assume a fixed universe. Note the audit cannot see *small* pool changes
(the documented 368 → 357 is ~3%, the same magnitude as monthly contract rollover
churn) — see `docs/modules/data_quality.md`.

## Other findings from the 2026-08-07 audit

- **`exch_ts` causality is clean table-wide**: 0 rows with `exch_ts > ingest_ts + 1s`
  across all 949 M rows, `max(exch_ts - ingest_ts)` = 46 ms. The 2026-08-05 repair of
  the +8h shift holds, and the two newly synced partitions did not disturb it.
- **`trade_direction` population is a hard family constraint** (Tick rows only):
  `202601`–`202603` = **0.0**, `202604` = 0.928, `202605` = 0.998, `202606` = 0.997,
  `202607` = 0.999, `202608` = 0.9997. Aggressor-split research cannot use Q1 at all.
- **5,593 duplicate `(symbol, exch_ts, ingest_ts, seq_no)` rows** on 5 days:
  `02-26` (426), `03-03` (7), `03-31` (144), `04-27` (1,387), `04-28` (3,629).
  Consistent with partial re-inserts — `market_data` is a plain MergeTree, so a
  re-import duplicates rather than replaces (see the pull sections above).

  **Not repaired, and here is exactly what that costs.** The source rows are left
  untouched; the exposure differs by consumer:

  | Consumer | Effect |
  |---|---|
  | `smma_dataset` OHLC | **none** — `argMinIf`/`argMaxIf` key on `(exch_ts, ingest_ts, seq_no)` and are idempotent under duplication |
  | `smma_dataset` `bar_trade_volume` | **inflated** — `sumIf(volume, type='Tick')` double-counts |
  | L2 tick export | **inflated** — the exporter's dedup (`DEDUP_WINDOW_NS`) is a *consecutive-identical-book* rule, not a key rule, and ticks get no dedup at all |

  Magnitude: the worst-affected day, `04-28`, has 3,629 duplicates against 23.7 M rows
  = **0.015%**. Documented rather than fixed because repairing it means mutating the
  only durable copy of the archive.
- **3,223,674 `BidAsk` rows carry empty depth arrays** (0.34% of the corpus), rising
  1.195% (Jan) → 6.433% (Aug). That trend is mostly **universe composition**, not
  degradation: restricted to `TXF`/`TMF` the rate is 0.04–0.55%, since one-sided books
  are normal for illiquid options and the universe grew 78 → 523 symbols. The one
  genuine anomaly is **July, where the futures-only rate spikes to 2.111%** against a
  0.544% June baseline — unexplained, and July is also the month holding the recording
  outage. Both exporters already exclude these rows.
- **`2026-04-03`: the tick channel published after the market closed.** The one day
  with rows outside a session window (17.3%, 198,678 rows). `exchange_calendars` marks
  XTAI **closed**; the `BidAsk` channel correctly stops at the 05:00 night close, but
  `Tick` keeps flowing 06:00 → 12:59 for 5–6 futures, decaying 88,959 → 4,804 rows/hour.
  These are not stale republishes: `TXFD6` alone shows 646 distinct prices and 17,446
  lots in hour 6. `exch_ts == ingest_ts` (lag 0), so `ts_causality` structurally cannot
  see them — `ts_session_window` is the only check that can, and it is what found them.

  The source table is **not** modified. The exporter is what changed: see
  `docs/modules/data_quality.md`. Measured end-to-end on `TXFD6`/`2026-04-03`, the
  session rule drops 32,629 of 146,323 rows (32,628 `Tick` + 1 `BidAsk`) and keeps the
  legitimate post-midnight night tail of trading day `2026-04-02`. 5,404 of those rows
  sit inside a valid clock window and are rejected **only** by the calendar half of the
  rule.

## Related

- `.agent/rules/70-research-data.md` — price scale and export contract
- `docs/operations/data-retention-policy.md` — retention intent
- `src/hft_platform/migrations/clickhouse/` — schema source of truth (still
  declares the TTL this table no longer has)
