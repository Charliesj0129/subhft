# TMFD6 Corpus — Inventory & Decision (2026-05-06)

**Author:** Charlie · **Status:** Decision locked · **Successor of:** none

## Problem

`vm_ul6_strict.yaml::thresholds.taker.min_oos_days = 30` is a Gate D blocker. c75 (`c75_tmf_mw_ofi_taker`) declares `instruments: [TMFD6]` and the canonical research backtest reads from `research/data/raw/tmfd6/TMFD6_*_l2.hftbt.npz`. We need ≥30 unique session-days available there.

## Disk-state inventory (verified 2026-05-06)

```bash
$ ls research/data/raw/tmfd6/ | grep -E '^TMFD6_[0-9-]+_l2\.hftbt\.npz$' | sort -u | wc -l
26
$ ls research/data/raw/tmfd6/ | grep -oE 'TMFD6_[0-9]+-[0-9]+-[0-9]+' | sort -u | wc -l
26
```

26 unique TMFD6 session-days, all already in canonical `*_l2.hftbt.npz` form.

**Date list:**

```
TMFD6_2026-01-26  TMFD6_2026-01-27  TMFD6_2026-01-28  TMFD6_2026-01-29  TMFD6_2026-01-30
TMFD6_2026-02-03  TMFD6_2026-02-04  TMFD6_2026-02-05  TMFD6_2026-02-06
TMFD6_2026-02-23  TMFD6_2026-02-24  TMFD6_2026-02-25
TMFD6_2026-03-19  TMFD6_2026-03-20  TMFD6_2026-03-23  TMFD6_2026-03-24
TMFD6_2026-03-26  TMFD6_2026-03-27  TMFD6_2026-03-30  TMFD6_2026-03-31
TMFD6_2026-04-01  TMFD6_2026-04-02  TMFD6_2026-04-07  TMFD6_2026-04-08
TMFD6_2026-04-13  TMFD6_2026-04-14
```

Gap of 4 days vs. the strict threshold.

## Plan-correction: there are 0 legacy-only TMFD6 dates

The original plan assumed "8 additional dates exist only as legacy `_ticks.npy`/`_bidask.npy` — total possible coverage = 34 if conversion succeeds." This is **false**. On disk:

| Date            | `_l2.hftbt.npz` | `_ticks.npy` | `_bidask.npy` |
| --------------- | --------------- | ------------ | ------------- |
| 2026-03-19..31  | YES (8 dates)   | YES          | YES           |

The 8 "legacy" pairs are duplicates that **already have** snapshot npz form (likely produced by an earlier export run). The other 8 legacy `*_ticks.npy`/`*_bidask.npy` files in the directory are **TXFD6** (not TMFD6) — wrong contract for c75.

**Consequence:** Step 3 of the parent plan (`data/tmfd6-legacy-snapshot-conversion`) is **SKIPPED**. There is nothing to convert.

## Locked corpus path = (B) CK top-up only

The only path to >=30 TMFD6 days is ClickHouse top-up. We need >=4 more unique TMFD6 session-days from `hft.market_data` not already in the on-disk corpus.

Constraint: TMFD6 only — no contract-month mixing (no TMFC6, no TXFD6 mixing). If TMFD6 alone cannot reach 30, document the gap; do NOT silently substitute another contract month or symbol.

## Step 4 execution criteria

1. Query CK for distinct dates available for `symbol='TMFD6'` *not* already in the on-disk list above.
2. If >=4 candidate dates returned:
   * Export each via `research/data/ck_export/export_golden.py` (or `.sh` wrapper) with `--depth-levels 5 --price-scale 1000000`.
   * Stamp meta sidecars with `make research-stamp-data-meta`.
   * Re-run the count verification — must be >=30.
3. If <4 candidate dates returned:
   * Document the actual count in this runbook (append "Update YYYY-MM-DD: TMFD6 corpus topped at N days, gap of M").
   * Continue to Step 8 with N<30 and accept that the `min_sample_size` sub-gate will hard-fail. The verdict will be `KILLED — min_sample_size`.

## Verification (post-Step-4)

```bash
ls research/data/raw/tmfd6/ | grep -E '^TMFD6_[0-9-]+_l2\.hftbt\.npz$' | sort -u | wc -l   # >=30
```

## Cross-references

* Parent plan: `~/.claude/plans/scalable-inventing-forest.md`
* npz format detail: `docs/runbooks/npz-formats-2026-05-06.md`
* Depth-parity decision: `docs/runbooks/c75-depth-parity-decision-2026-05-06.md`
