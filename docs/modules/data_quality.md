# data_quality — Source Data Quality Audit

> **Module**: `research/data_pipeline/quality.py`
> **CLI**: `python -m research data-pipeline quality` / `make research-data-quality`

## Overview

Audits the **raw ClickHouse `hft.market_data` table** — the layer upstream of every
research dataset. It is offline, read-only, and **advisory**: it produces a verdict and
a report, and never blocks an export.

This exists because everything downstream already had governance (artifact-contract
validation in `research/data_pipeline/__init__.py`, a rich per-dataset sidecar in
`research/combinatorial/smma_dataset.py`) while the source table had none — which is
how the `exch_ts` +8h shift over partitions `20260126`–`20260205` survived roughly six
months undetected.

> **Historical note.** This document previously described a
> `src/hft_platform/data_quality/profiler.py` exporting `DataProfiler`. That package
> was never tracked in git and no longer exists on disk. It has been replaced by the
> module above, which lives in `research/` because it audits research data, not a hot
> path.

## Checks

| Check | Severity | What it catches |
|---|---|---|
| `ts_causality` | error | `exch_ts` ahead of `ingest_ts` — physically impossible. The invariant the +8h shift broke |
| `ts_session_window` | warn | Rows outside TAIFEX sessions (08:45–13:45 / 15:00–05:00); catches shifts in the direction causality cannot see |
| `price_sanity` | error | Non-positive trade prices, negative bids |
| `book_crossed` | warn | Best ask below best bid |
| `depth_shape` | warn | Price/volume array length mismatch, empty `BidAsk` rows |
| `duplicate_keys` | warn | Repeated `(symbol, exch_ts, ingest_ts, seq_no)` — `market_data` is a plain MergeTree, so a re-import duplicates rather than replaces |
| `coverage_profile` | warn | Per-day `clean` / `partial` / `degraded` / `missing`, against a **local** baseline (median of a centred 11-day window) so a genuine universe step does not condemn an era |
| `universe_drift` | warn | Day-over-day symbol-universe steps |
| `field_coverage` | info | Per-month `trade_direction` population and event-type mix |
| `archive_sync` | warn / **error** | Partitions present upstream but missing from the local archive. See below |
| `eligibility` | info | Delegated to `research.combinatorial.taifex_trading_dates.full_session_eligibility`; reported `unavailable` when that module is not importable |

Verdict: any failing `error` → `BROKEN`; else any failing `warn` → `DEGRADED`; else
`CLEAN`.

## `archive_sync` — the only check with dynamic severity

The local corpus is the **only durable copy** of research market data (its TTL was
removed 2026-07-25), while the upstream production table still enforces a 6-month TTL.
A partition missing locally is therefore recoverable *until it is not*.

- `warn` — behind, but the upstream copy still exists with runway.
- `error` — missing locally **and** the upstream copy expires within
  `ARCHIVE_SYNC_URGENT_HORIZON_DAYS` (30). This is the window in which the loss becomes
  irreversible, and it is the only condition under which being behind should turn the
  whole audit `BROKEN`.
- `unavailable` — no reference inventory was supplied; the detail names the producer.

The check is **offline**: it never connects to the upstream host. It compares the local
`system.parts` inventory against a JSON reference emitted separately:

```bash
make research-archive-sync     # writes research/reports/data_quality/theshow_inventory.json
make research-data-quality DATE_FROM=2026-01-26 DATE_TO=2026-08-06 \
  ARGS='--reference-inventory research/reports/data_quality/theshow_inventory.json'
```

Row-count deltas on shared partitions are reported separately from missing partitions,
because they are **not** repairable the same way: `market_data` is a plain MergeTree, so
re-pulling a partition duplicates rather than replaces it.

## Session filtering in the L2 exporter

The audit found one day — `2026-04-03` — where the tick channel kept publishing real
trades for seven hours after the market closed (`ts_session_window`, 198,678 rows).
`research/data_pipeline/__init__.py` previously had no session filter at all, so those
rows would have been exported as a legitimate trading day.

`is_session_row()` / `filter_session_rows()` now apply two independent conditions:

| Row's Taipei clock | Kept when |
|---|---|
| Day window 08:45–13:45 | **this** date is an XTAI session |
| Night window 15:00–05:00 | 15:00–23:59: this date is a session · 00:00–05:00: the **previous** date is |
| Anything else | never |

Both halves are load-bearing. On `2026-04-03`, 06:00–08:44 falls between the windows,
but 08:45–12:59 sits squarely *inside* the day window — only the calendar can reject it.
Measured on `TXFD6`/`2026-04-03`: 32,629 of 146,323 rows dropped, of which **5,404 are
rejected by the calendar half alone**, while the legitimate post-midnight night tail of
trading day `2026-04-02` survives intact.

Filtering is done in Python rather than SQL so the rule has exactly one implementation
and can be tested against the real defect shape without a running ClickHouse
(`tests/research/test_export_contamination.py`). Each artifact records
`session_filtered_rows` and `session_rule` in its sidecar, on the same principle as the
source-quality stamp: what was excluded is stated, not silent. `--allow-non-session`
relaxes the calendar half only; the clock windows always apply.

Trading-day *eligibility* remains delegated (below). Asking "is this date a session" is
a plain calendar lookup and is not the same question.

## Why eligibility is delegated, never recomputed

The 5-day-bar / 14-night-bar full-session rule has exactly one authority. An SQL
re-implementation of it produced 92 and then 33 eligible days where the correct answer
was 60. The auditor reports `unavailable` rather than approximating.

## Provenance stamping

`load_latest_report()` + `stamp_payload()` merge `source_quality_*` keys into dataset
sidecars, so every research artifact records the data-quality state it was built on:

```json
{
  "source_quality_schema": "hft_source_quality.v1",
  "source_quality_verdict": "DEGRADED",
  "source_quality_report_sha256": "…",
  "source_quality_range": ["2026-01-26", "2026-08-05"],
  "source_quality_findings": ["coverage_profile:…", "universe_drift:…"]
}
```

The verdict is always a concrete string: `unstamped` when no report exists, and
`unstamped_range_mismatch` when the newest report does not cover the dataset's
requested range. A missing audit is visible in the artifact, never silently absent.

Stamp points:

- `research/data_pipeline/__init__.py` `_write_day_outputs` — L2/tick exports
- `research/combinatorial/smma_dataset.py` `save_governed_dataset` — mining bar
  datasets (on the mining branch; merged before `metadata_hash`, so pre-existing
  sidecars still verify)

## Usage

```bash
make research-data-quality DATE_FROM=2026-01-26 DATE_TO=2026-08-05

# advisory by default; opt into an exit code
python -m research data-pipeline quality \
  --date-from 2026-01-26 --date-to 2026-08-05 --fail-on error
```

Reports are written to `research/reports/data_quality/<timestamp>_source_audit.{json,md}`.

Aggregates run in day-chunks (`--chunk-days`, default 4) and halve automatically on
ClickHouse memory pressure — the per-day `uniqExact` states used for exact duplicate
detection do not fit the whole corpus in one query.

## Related

- `.agent/rules/70-research-data.md` — price scale and export contract
- `docs/operations/local-clickhouse-market-data-corpus.md` — corpus provenance and coverage
