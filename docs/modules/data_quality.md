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
| `eligibility` | info | Delegated to `research.combinatorial.taifex_trading_dates.full_session_eligibility`; reported `unavailable` when that module is not importable |

Verdict: any failing `error` → `BROKEN`; else any failing `warn` → `DEGRADED`; else
`CLEAN`.

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
