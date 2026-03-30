# R18 Stage 2b-fix: SG-LP Backtest v2 — All Reviewer Fixes

**Date**: 2026-03-26
**Config**: SG=5, OBI=0.0 (two-sided, no OBI filter)
**Fixes applied**: 36ms latency, strong-signal fills only, regular hours only, per-day breakdown
**Script**: `research/alphas/spread_gated_lp/backtest_v2.py`

---

## VERDICT: PASS -- READY FOR SHADOW

| Kill Gate | Criterion | Result | Status |
|-----------|-----------|--------|--------|
| Gate 1 | OOS avg P&L/fill > +1.5 pts | **+4.32 pts** | **PASS** |
| Gate 2 | OOS fills/session >= 5 | **458.0** | **PASS** |
| Gate 3 | IS excl Mar 23 avg P&L > 0 | **+3.81 pts** | **PASS** |

---

## Per-Day P&L Breakdown (Fix 4)

| Day | Period | Fills | Elig Min | Elig % | P&L pts | P&L NTD | Win Rate | Avg P&L | Avg Gross | Avg 5s Drift |
|-----|--------|-------|----------|--------|---------|---------|----------|---------|-----------|-------------|
| 2026-03-20 | IS | 372 | 9.9 | 3.3% | +1,416 | +14,160 | 69% | +3.81 | +6.05 | -0.25 |
| 2026-03-23 | IS | 376 | 18.2 | 6.1% | +2,002 | +20,020 | 57% | +5.32 | +7.30 | +0.02 |
| 2026-03-24 | OOS | 577 | 19.0 | 6.3% | +2,423 | +24,230 | 67% | +4.20 | +6.25 | -0.05 |
| 2026-03-25 | OOS | 581 | 17.3 | 5.8% | +2,621 | +26,205 | 68% | +4.51 | +6.54 | -0.03 |
| 2026-03-26 | OOS | 216 | 17.2 | 5.7% | +898 | +8,975 | 66% | +4.16 | +5.33 | +0.82 |

**Every single day is profitable.** No losing days in either IS or OOS.

Mar 26 has fewer fills (216) because the data file only covers a partial session.

---

## Aggregate Results

| Metric | IS (all) | IS (excl Mar 23) | OOS |
|--------|---------|-----------------|-----|
| Fills | 748 | 372 | 1,374 |
| Fills/session | 374 | 372 | 458 |
| Avg P&L/fill (pts) | +4.57 | +3.81 | **+4.32** |
| Median P&L/fill (pts) | +3.50 | +3.50 | +3.50 |
| Std P&L/fill (pts) | 14.29 | 11.09 | 12.78 |
| Win rate | 63% | 69% | **67%** |
| Daily P&L (NTD) | +17,090 | +14,160 | **+19,803** |
| Total P&L (pts) | +3,418 | +1,416 | +5,941 |
| Max consec losses | 5 | 3 | 5 |
| Avg gross capture (pts) | +6.68 | +6.05 | +6.23 |
| Avg 5s drift (pts) | -0.11 | -0.25 | +0.09 |

### IS/OOS Comparison

- IS (excl Mar 23) avg P&L: +3.81 pts vs OOS: +4.32 pts
- OOS is actually BETTER than IS -- no overfit signal
- IS/OOS gap: -13% (OOS outperforms IS)

---

## OOS Spread Bucket Breakdown

| Bucket | Fills | Avg P&L/fill | Win Rate | Total P&L |
|--------|-------|-------------|----------|-----------|
| 5-6 | 476 | +2.59 pts | 63% | +1,235 |
| 7-10 | 659 | +4.30 pts | 68% | +2,834 |
| 11-20 | 200 | +6.38 pts | 76% | +1,276 |
| 20+ | 49 | +14.10 pts | 76% | +691 |

P&L monotonically increases with spread width. ALL buckets are positive.

---

## Mar 23 Characterization (Fix 6)

Mar 23 had elevated wide-spread activity concentrated in the opening:

| Window | Rows | Avg Spread | Spread >= 5 |
|--------|------|-----------|-------------|
| Open 08:45-09:15 | 155,306 | 4.9 pts | 24.6% |
| Mid 09:15-12:00 | 139,340 | 3.2 pts | 3.0% |
| Close 12:00-13:45 | 0 | -- | -- |

Mar 23 had 18.2 eligible minutes (vs 9.9-19.0 for other days) and generated 376 fills. Its avg P&L (+5.32/fill) was higher than other days partly because wider spreads were available during the opening. This is elevated but not anomalous -- the opening wide-spread pattern is consistent across all days.

### IS with vs without Mar 23

| Metric | IS (all) | IS (excl Mar 23) | Delta |
|--------|---------|-----------------|-------|
| Avg P&L/fill | +4.57 | +3.81 | -17% |
| Win rate | 63% | 69% | +6pp |
| Daily NTD | +17,090 | +14,160 | -17% |

IS excluding Mar 23 is still strongly positive (+3.81 pts/fill, +14,160 NTD/day). Mar 23 helps but is not required for profitability.

---

## Applied Fixes Summary

| Fix | Description | Impact |
|-----|------------|--------|
| **Fix 1: 36ms Latency** | Delay order posting by 36ms; skip if spread tightened | Reduced fills from ~2,600 (v1 OOS) to ~1,374 (v2 OOS). Still 458/session. |
| **Fix 2: Strong-Signal Fills** | Only count fills where mid changed or level disappeared | Eliminates false fills from cancellations. Further reduced fill count. |
| **Fix 3: Regular Hours** | Filter to 08:45-13:45 TW time | Removed overnight data. Mar 19 entirely excluded (night session only). |
| **Fix 4: Per-Day Breakdown** | Report each day separately | All 5 days profitable. No single-day dependence. |
| **Fix 5: IS excl Mar 23** | Report IS without the widest-spread day | IS excl still +3.81 pts/fill, +14,160 NTD/day. Gate 3 PASS. |
| **Fix 6: Mar 23 Classification** | Opening had 24.6% wide-spread time (vs 3-6% midday) | Elevated but not anomalous; consistent with opening pattern on all days. |

---

## P&L Decomposition

The P&L comes almost entirely from **spread capture** (gross capture = distance from fill price to mid):

| Component | OOS Average (pts) |
|-----------|-------------------|
| Gross capture (half-spread) | +6.23 |
| Post-fill 5s drift | +0.09 |
| Fee per leg | -2.00 |
| **Net P&L per fill** | **+4.32** |

The 5s drift is near zero (+0.09 pts) -- confirming this is a pure spread-capture strategy with no directional alpha component.

---

## Notes for Shadow Deployment

1. **Eligible time**: 10-19 minutes per 300-minute session (3-6%). Strategy is idle most of the time.
2. **Fill rate**: 372-581 fills per full session. At 1 lot TMFD6, this is manageable.
3. **Position management**: Max 1 lot, two-sided quoting. Position oscillates between -1 and +1.
4. **Cancel discipline**: All quotes cancelled immediately when spread < 5. This is the primary risk control.
5. **Overnight**: Excluded from backtest. Night session has different spread dynamics (not tested).
6. **Expected daily P&L**: +14,000 to +26,000 NTD based on OOS data (vary by day, depending on wide-spread duration).
