# Round 2 Summary — V2 Adaptive Spread Gate Conditional on Quote-Lifetime Regime

**Run**: `alpha-research-20260424-r47-revalidation`
**Round**: 2
**Candidate**: V2 — Adaptive spread gate conditional on quote-lifetime regime (axis A_spread, priority 2)
**Instrument**: TMFD6
**Verdict**: **KILL** (T2 REJECT, S3 FAIL + Survivor §1 FAIL)
**Dates**: 2026-04-24 T0..T2
**Consecutive kills after this round**: 2

## Pipeline stages

| Stage | Owner | Duration | Verdict |
|-------|-------|----------|---------|
| T1 | researcher | ~7 min | PROCEED (trichotomy (c) — trades fills for per-fill edge, 17 fills/31d clears §6 barely) |
| T2 | devils-advocate | ~4 min | **REJECT — S3 FAIL + Survivor §1 sign-robustness FAIL** |
| T4-T7 | — | SKIPPED | no revision loop per task chain |

## Artifacts

- `docs/alpha-research/round-2-r47-revalidation/artifacts/t1_researcher_v2.md`
- `docs/alpha-research/round-2-r47-revalidation/artifacts/t2_devils_advocate_v2.md`

## Kill reasoning

V2 proposed an asymmetric one-sided gate: `sp_fast=5` in fast-regime (quote lifetime P95 < 420ms), `sp_slow=7` in slow-regime (quote lifetime P95 ≥ 420ms), where regime is classified by causal rolling 5-min window on own-order lifetime. Researcher reformulated from pool's literal "lower sp in fast regime" because literal sp=3 violates cost floor (drag 133%).

Two independent mechanisms kill V2, both deterministic from existing audit data:

### 1. S3 FAIL via Risk #4 dichotomy

The +259 NTD / 31d V2 headline is driven entirely by importing the V1 sp=7 audit cell (+1,468 slow-regime NTD on 9 fills / 1 winning day / +36.7 pt/fill). Under EITHER Risk #4 scenario V2 dies:
- **Scenario A — winning day is slow-regime**: direct jackknife flip. Policy §3 (jackknife survive) + §4 (max_day ≤ 25%) + §5 (winning_days ≥ 5) all fail simultaneously.
- **Scenario B — winning day is fast-regime**: the +36.7 pt/fill figure should NOT have been transferred to V2's slow-regime cell at all. V2 realizes at ~−1,209 NTD (pure fast-regime residual = same as pre-winning-day V1 sp=5).

No third scenario rescues V2.

### 2. Survivor §1 sign-robustness FAIL independently

V2 at 210ms = +259 NTD / 31d flips to −696 NTD / 800ms within the mandatory sensitivity band. The +955 NTD swing across profiles, same strategy / same data, is verdict-flipping by itself. V2 is doubly killed: sign-robustness alone would have been sufficient.

## H/S scoreline (T2 Kill Checklist)

| Tier | Check | Verdict |
|------|-------|---------|
| S0 | Scope | PASS (pure_maker in allowed_types) |
| H1 | Cost arithmetic | PASS with pivot note (sp_fast=5 / sp_slow=7 asymmetric gate due to cost-floor violation at sp=3) |
| H2 | Spread vs edge | PASS-with-bright-line-WARN (drag 80% fast, 57% slow) |
| H3 | Killed-direction overlap | PASS (charitable — own-order lifetime is mechanism-distinct from L1 imbalance; non-demonstration of adverse change flagged to Lead) |
| H4 | Data sufficiency | PASS (31d role-template floor of 20d) |
| H5 | Latency feasibility | PASS (quote lifetime 32s vs 420ms = 76× margin) |
| H6 | Execution model | PASS |
| S1 | IC detrending | PASS (no IC used) |
| S2 | OOS validation | PASS |
| **S3** | **Sample size** | **FAIL — mechanical via Risk #4 dichotomy; +259 NTD headline dies in both scenarios** |
| S4 | Recency bias | PASS |
| S5 | Paper-to-code fidelity | PASS |
| S6 | Regime dependency | WARN (regime-split assumption unverified on actual data) |
| Survivor §1 | Sign-robustness | **FAIL — 210ms +259 flips to 800ms −696 within sensitivity band** |

Tier 1 FAIL: 0. Tier 2 FAIL: 1 (S3, mechanically strengthened). Verdict: **REJECT**.

## Researcher's three research-process asks (DA-scored)

- **Risk #3 (causal gap in regime label)**: PASS — design is causal-correct; T5 must verify no leakage.
- **Risk #4 (winning-day regime classification)**: WARN — answerable from audit data but MOOT; kills V2 under either classification.
- **Risk #6 (actual days_slow / days_fast split from data)**: WARN — answerable but does not rescue verdict.

## Comparison to R1 V1

| Axis | V1 (R1) | V2 (R2) |
|---|---|---|
| Fills/day at best cell | 0.29 (sp=7) | 0.55 (blended) |
| PnL 210ms / 31d | +3,302 (sp=7, killed on jackknife) | +259 (killed on Risk #4 dichotomy) |
| PnL 800ms / 31d | +2,323 extrapolated (sp=7, sign-robust) | −696 (sign-flip) |
| Implementation | drop-in extension of existing sweep_r47_spread.py | +40-60 LOC new strategy extension |
| Mechanism | fixed sp | regime-adaptive sp |
| Kill source | sp=7 cell 1-winning-day dominance | imports same sp=7 cell pathology + sign-flip |

**V2 is strictly weaker than V1**: same winning-day dependency + additional sign-robustness failure + more implementation complexity for predetermined kill.

## Pool implication (DA meta-finding)

Spread-axis (A) candidates are now **2-for-2 killed on 31d + jackknife discipline**, both dying on the same V1 sp=7 audit cell being sample-insufficient. **Any future A-axis variant that imports that cell's +36.7 pt/fill figure will inherit the same kill mechanism.**

This has implications for T8-REGEN if the pool runs low — any A-axis regen candidate would need to SIDESTEP the sp=7 audit cell entirely, which is hard because sp=7 is the only 31d cell in the sweep with non-trivial historical fills.

## Lead observations

1. DA's verdict structure continues the R1 addendum innovation: mechanical proof from existing audit data, without requiring a new backtest. This is the epistemic signature of a well-designed review pipeline — the kill is deterministic on disk, not speculative.
2. V2's Risk #4 dichotomy is a beautiful piece of kill-reasoning. It shows that under ANY resolution of the regime-label question, the candidate dies. This is stronger than a single-path kill.
3. Researcher's honest probability estimates (P=0.60 S3 jackknife fail, P=0.20 narrow positive, P=0.10 surprise) match the outcome (kill). She's self-diagnosing accurately, which is the right epistemic shape.
4. The pattern forming across R1+R2: **31d data ceiling is structurally incompatible with TMFD6 retail R47 variant revalidation via sweep or regime-split mechanisms.** The only variants that could break this pattern would change the fill-rate population itself (V3 cross-instrument, V4 own-order control, V5 L2-depth filter). All three have reasons to also fail — but they fail via different mechanisms, which is the research-significant distinction.

## Next round

R3 pops **V3 — TXF-TMF delta-neutral pair maker with cancel-cadence-aware hedge** (axis B_hedge, priority 3). DA has pre-flagged H3 re-admit criteria for `txf-tmf-passive-pair-maker-1-5-hedge`:
- (a) corrected 1:20 delta-neutral ratio arithmetic (prior kill used 1:5 which was wrong)
- (b) cancel-cadence-aware hedge-path latency (hedge-leg adds another 210ms round-trip)

Without both, H3 FAIL immediately. T1-R3-V3 dispatched to researcher.
