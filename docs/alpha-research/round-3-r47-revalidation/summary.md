# Round 3 Summary — V3 TXF-TMF Delta-Neutral Pair Maker

**Run**: `alpha-research-20260424-r47-revalidation`
**Round**: 3
**Candidate**: V3 — TXF-TMF delta-neutral pair maker with cancel-cadence-aware hedge (axis B_hedge, priority 3)
**Instruments**: TMFD6 + TXFD6
**Verdict**: **T1 SELF-KILL** (no DA dispatch; kill is mathematical not empirical)
**Dates**: 2026-04-24 T0..T1
**Consecutive kills after this round**: 3

## Pipeline stages

| Stage | Owner | Duration | Verdict |
|-------|-------|----------|---------|
| T1 | researcher | ~5 min | **SELF-KILL on K1 (H3a ratio-derivation) + K2 (H1 cost at realistic sp)** |
| T2-T7 | — | SKIPPED | self-kill at T1 per task chain |

## Artifact

- `docs/alpha-research/round-3-r47-revalidation/artifacts/t1_researcher_v3.md`

## Kill reasoning (two independent structural fails)

### K1 — H3(a) ratio-derivation fails

Pool description ("1 TMFD6 contract hedged by 20 TXFD6 contracts at 1:20 point-value-neutral ratio") and Lead's Task #6 description had the delta-neutral ratio **INVERTED**. Correct derivation from point-value ratio:

```
N_txf / N_tmf = point_value_tmf / point_value_txf = 10 / 200 = 1/20

=> Correct delta-neutral ratio is 20 TMFD6 : 1 TXFD6 (i.e. 0.05 TXFD6 per TMFD6)
```

The pool's "1 TMFD6 : 20 TXFD6" over-hedges by 400× and produces delta-SHORT-TXF, not delta-neutral. At integer contract granularity, 0.05 TXFD6 per TMFD6 fill is not representable → hedge requires 20-TMFD6-fill batching.

At TMFD6 sp=5 (0.81 fills/day per audit), a 20-fill batch takes ~25 days. At sp=7 (0.29 fills/day), ~70 days. During any batch, position is delta-ACCUMULATED not delta-neutral. **The mechanism cannot be instantiated as "delta-neutral pair maker" at retail 1-lot fill rates.** H3(a) re-admit criterion (corrected ratio + mechanism executable) fails.

### K2 — H1 cost arithmetic fails at realistic sp

Per-fill net:
- sp=5: −163 NTD = (−9.3 × 10 maker gross) + (−40 maker cost) + (−30 amortized hedge cost from batched 20:1 ratio)
- sp=7: +297 NTD = (+36.7 × 10 maker gross) + (−40 maker cost) + (−30 amortized hedge cost)

The only positive cell (sp=7) depends on:
1. The SAME single-day-dominated +36.7 pt/fill audit figure that killed V1 at jackknife (R1 KILL) and drove V2's kill (R2 KILL) — cross-candidate H2 pattern.
2. Only 9 fills in 31d < 20-fill batch size required for delta-neutral hedge firing → **hedge is DORMANT in the 31d sample**. V3 at sp=7 is operationally "V1 sp=7 + unused hedge infrastructure".

### Trichotomy answer

**(b)** V3 inherits V1's fill sparsity. Hedge cycles per 31d: 1.25 at sp=5, 0.45 at sp=7. §6 floor (≥15) unmet for hedge cycles under any sp. Per Task #6 description: "(b) → SELF-KILL at T1". Executed.

## Lead napkin correction (explicit record)

Lead's pre-napkin "hedge cost 12,000 NTD/fill / 20 NTD maker gross = 600× dominance" was directionally correct as catastrophe verdict but had a unit-scaling/ratio-inversion error:
- Under pool-literal (wrong) 1:20 interpretation: cost = 12,000 NTD/fill, which IS catastrophic.
- Under correct batched 20:1 interpretation: cost = ~30 NTD/fill amortized, which is NOT 600× dominant.
- The 600× number was wrong direction; the kill verdict was right.

Researcher caught this in her T1 and the correct analysis stands. The conclusion (V3 dies) is unchanged but the reasoning chain belongs to Researcher's T1, not Lead's pre-napkin.

## H3 re-admit criteria audit

Killed direction `txf-tmf-passive-pair-maker-1-5-hedge` (20260419-R7) required:
- (a) corrected 1:20 delta-neutral ratio arithmetic — **FAILS** (actual ratio is 20:1, not 1:20; pool description was wrong)
- (b) cancel-cadence-aware hedge-path latency — moot; hedge never fires at sample scale

Neither criterion satisfied. Candidate re-admit formally denied.

## Cross-candidate H2 finding (emerging pattern — Researcher-flagged)

Across R1 V1, R2 V2, R3 V3, the positive-economics envelope uniformly traces back to the same audit cell:
- **V1 sp=7**: +36.7 pt/fill / 9 fills / 1 winning day / 31d
- **V2 slow-regime subset**: imports V1 sp=7 figure as slow-regime proxy
- **V3 sp=7 (pre-hedge)**: imports V1 sp=7 figure as the only positive cell

When R1 DA applied §3 jackknife on that cell, the entire positive-economics envelope for V1/V2/V3 collapsed simultaneously. This is not three independent kills — it is ONE audit-cell dependency exposed three ways.

**V4 and V5 are on different axes** (queue-age cancel, L2 thin-queue filter). If V4/V5 also end up depending on the same sp=7 audit economics, cross-candidate H2 becomes the structural finding of this run: **"On 31d TMFD6 retail, the R47 variant-space has exactly one positive-economic cell and it is jackknife-non-survivable."**

If V4/V5 depend on distinct economics (different fill selection, different adverse profile), the finding instead is: **"R47 variants fail via axis-specific mechanisms but none survive 31d jackknife."** Both outcomes are research-significant.

## Budget state

- Runtime used: ~45 min of 24h (~3%)
- Rounds: 3/20
- Promotes: 0/3
- Consecutive KILLs: 3/8
- Pool remaining: V4, V5 (V6-shelved, V1/V2/V3 killed)

## Next round

R4 pops **V4 — Queue-age-aware cancel cadence** (axis C_queue-position, priority 4). Expected structure: cancel control reduces fills strictly vs baseline → V4 cannot raise fills above V1 sp=5's 25/31d. Kill mode likely: §6 compliance at V4's reduced fill rate AND jackknife on whichever positive cell V4 inherits (possibly same sp=7 cell, possibly not depending on regime-adaptive cancel behavior).

DA has pre-framed the axis-C test: "Does own-order cancel control produce enough fill-count change to escape sparsity?" (expected answer: no).
