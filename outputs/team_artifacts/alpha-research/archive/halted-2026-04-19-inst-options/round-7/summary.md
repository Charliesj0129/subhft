# R7 Summary — C66 TXF-TMF Passive Pair MM

**Run**: alpha-research-20260419-inst-options | **Round**: R7 | **Date**: 2026-04-19

## Verdict: KILL (Researcher SELF-KILL at T1, with 1:20 correction)

Initial dispatch used wrong hedge ratio (1:5); DA caught at T2 pre-load. Researcher redid physics with correct 1:20 notional-neutral ratio. All 3 self-KILL criteria trigger MORE emphatically under correction.

## Kill scorecard (1:20 corrected)

| Scenario | Per-trip NET | Mechanism |
|---|---:|---|
| A (both-maker) | -260 NTD | Both half-spreads captured; still negative |
| B (TXF maker + 20 TMF take-hedge) | -730 NTD | Take-hedge cost dominates |
| **B' (realistic 20 TMF maker + 1 TXF take-hedge)** | **-940 NTD** | Physics root cause |
| Sensitivity ±30% | -880 to -1,000 NTD | No sign flip anywhere |

Physics root cause: TXF hedge-take cost = 4.35 pt × 200 NTD/pt = **870 NTD**/hedge vs TMF maker gross **50 NTD**/cycle. Hedge cost 9-17× TMF gross.

## New structural concern under 1:20

20-TMF quote **dominates TMFD6 L1 queue depth** (median 3-4 contracts). Our quote IS 5-7× the typical queue → adverse-selection bias amplified beyond 1.6pt baseline.

## Joint CK-direct analysis (instrument-matched per R2-SUPP lesson)

- 16,013 simultaneous TXFD6+TMFD6 minutes (2026-03-19 → 2026-04-14)
- Mid-price correlation: 0.9999 (essentially identical)
- Basis mean +0.39 pt; per-day stdev 1.84-23.17 pt (non-stationary)
- 0.9999 correlation → same-direction flow → opposite-direction simultaneous passive fills rare

## Process lesson (record for shared-context)

Researcher internalized: "Always derive dollar-neutral ratio independently from shared-context point_value specs; cross-check against kill-class history (e.g., R5-prior C30 '20× undercount' IMPLIES correct ratio IS 20×, not 1×). Future candidates: compute ratios from first principles before accepting dispatch numbers."

This extends R2-SUPP lesson (instrument-matched data) to "physics-first-principles" for all hedge/cross-instrument math.

## Kill class (new for shared-context)

```yaml
- id: "txf_tmf_passive_pair_maker_hedge_take_cost_dominant"
  rounds: "R7-C66"
  reason: "All 3 self-KILL criteria trigger at 1:20 corrected physics. Scenario B' (realistic 20-TMF maker + 1-TXF hedge) = -940 NTD/trip. TXF hedge-take cost (870 NTD) dominates TMF maker gross (50 NTD) by 17×. 20-TMF quote size dominates median L1 queue depth (3-4 contracts), amplifying adverse selection. Not rescued by inst-tier cost reduction."
  re_admit: "stay_killed"
  note: "Salvage lead: basis-mean-reversion at |basis|>2σ could work (+12K linear/day) but per-day σ non-stationary (1.84-23.17 pt) = session-variant per R14-prior. Future C66b distinct candidate possible."
```

## C72 T5 late-reveal (R5 unchanged, informational only)

Executor delivered C72 T5 backtest after R5 KILL close. 8 of 9 (depth, mp) configs DOMINATED by C60. Only depth=2/mp=3 marginally beat C60 (+974 NTD/day, +23.7% per-trip). Executor flags "single data point with overfitting risk". R5 KILL verdict stands (1/9 is standard noise); depth=2/mp=3 sub-variant noted for future re-admit consideration.

## Budget status after R7

- Rounds: 7/20
- PROMOTEs: 2/3 (C60, C63)
- KILLs: 5 (C64, C68, C72, C71, C66)
- Consecutive kills: 4 (C68→C72→C71→C66; since R2-SUPP PROMOTE)
- Runtime: ~3h of 24h
- Pool remaining: C65, C67, C70 (3 candidates, all lead-flagged likely-fail)
- 1 PROMOTE slot remaining

## Next: R8 pops C70 (TXO ATM single-leg maker)

Rationale:
- Validates C-precondition infra end-to-end (only TXO candidate we can run)
- Even if KILL (expected per lead_flag H1 FAIL), informative for any future TXO work
- Researcher + Lead aligned
- After C70, pool has only C65/C67 left (both lead-flagged likely-fail); T8-REGEN likely needed
