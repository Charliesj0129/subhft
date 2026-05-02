# Round 4 Summary — V4 Queue-age-aware Cancel Cadence

**Run**: `alpha-research-20260424-r47-revalidation`
**Round**: 4
**Candidate**: V4 — Queue-age-aware cancel cadence (axis C_queue-position, priority 4)
**Instrument**: TMFD6
**Verdict**: **KILL** (T2 REJECT, H2 + S5 + Survivor §1 FAIL)
**Dates**: 2026-04-24 T0..T2
**Consecutive kills after this round**: 4

## Pipeline stages

| Stage | Owner | Duration | Verdict |
|-------|-------|----------|---------|
| T1 | researcher | ~6 min | **PROCEED** (trichotomy (β) — distinct economics, first candidate to escape cross-candidate H2) |
| T2 | devils-advocate | ~5 min | **REJECT — H2 + S5 + Survivor §1 FAIL** (three independent tier-1/2 fails) |
| T4-T7 | — | SKIPPED | no revision loop per task chain |

## Artifacts

- `docs/alpha-research/round-4-r47-revalidation/artifacts/t1_researcher_v4.md`
- `docs/alpha-research/round-4-r47-revalidation/artifacts/t2_devils_advocate_v4.md`

## What V4 proposed

Queue-age cancel timer T (scanned T ∈ {30, 45, 60, 90, 120}s). V4 operates on sp=5 baseline (V1 baseline, 25 fills / 31d at -2,332 NTD) and layers a cancel trigger: if own order has been in-queue longer than T seconds, cancel and re-post fresh. Mechanism rationale: short-lifetime retained fills should have less adverse selection than long-lifetime retained fills. Researcher modelled a linear adverse-vs-lifetime slope of 0.2 pt/s inferred from V1 audit decomposition.

## Researcher's T1 arithmetic (31d, sp=5 base, sweep T)

| T (s) | fills/31d | edge/fill pt | 31d NTD @210ms | §6 (≥15)? |
|---:|---:|---:|---:|---|
| 30  | 14.5 | −2.5 | −363   | **FAIL** |
| **60** | **21** | **−5.5** | **−1,163** | PASS (+6) |
| 90  | 23   | −8.5 | −1,949 | PASS |
| 120 | 24   | −9.3 | −2,240 | PASS (≈V1 sp=5 baseline) |

800ms sensitivity at T=60s: **−1,894 NTD** (sign-robust NEGATIVE, magnitude 63% worse).

Researcher flagged this as "mechanism-valid but cost-defeated" — a legitimate research-significant negative result (P=0.70 T5 outcome estimate).

## Kill reasoning (DA T2)

Three independent mechanical fails:

### 1. Survivor §1 direct-text FAIL

Policy §1 reads: "PnL > 0 under v2026-04-24 (210ms) AND sign-robust at canned 800ms". V4 at T=60s primary = −1,163 NTD, which fails the conjunct (a) directly. Sign-robust-negative does NOT satisfy §1 — §1 is "positive AND sign-stable", not "sign-stable regardless of sign". Mechanical from Researcher's own projection.

### 2. H2 FAIL — edge below cost floor

At T=60s primary cell: per-fill edge = -5.5 pt gross maker edge, retail RT = 4 pt, so net per-fill PnL = -5.5 − 4 = -9.5 pt per fill × 10 NTD/pt × 21 fills = -1,995 NTD mechanical lower bound (actually more favorable than reported -1,163 due to net-of-rebate-ish effects, but still negative). **Every cell in V4's sweep has net per-fill PnL negative at retail cost tier.** V4 does not cross zero at any T.

### 3. S5 FAIL — linear adverse-vs-lifetime slope not measured

V4's entire per-fill PnL formula embeds `0.1 × T` as a coefficient where the 0.1 slope is inferred from a 2-point anchor (V1 sp=5 decomposition at ~5s and ~80s points), NOT directly measured. Researcher explicitly flags this as "HYPOTHESIS not a measurement" in her own §Risk #4. Role-template S5 text: "Formula diff between paper and code without explicit justification = REJECT". The formula IS the mechanism; the slope IS the mechanism's quantitative claim.

### Alternative path explicitly logged and NOT taken

DA flagged that the task description offered "APPROVE-to-T3 for empirical adverse-vs-lifetime slope measurement" as a Lead-level path. DA correctly identified this as **re-labeling the primary deliverable** — converting V4 from "produce positive PnL" to "measure slope scientifically" sidesteps H2/S5/§1 by changing the success criterion. This is epistemically valid as research but not as alpha-promotion, and the /alpha-research run framing is the latter. Lead did NOT override — DA's REJECT stands.

## H/S scoreline (T2 Kill Checklist)

| Tier | Check | Verdict |
|------|-------|---------|
| S0 | Scope | PASS (pure_maker in allowed_types) |
| H1 | Cost arithmetic | PASS (retail RT=4pt applied correctly by Researcher) |
| **H2** | **Spread vs edge** | **FAIL — V4 per-fill edge −5.5 to −9.3 pt < retail 4pt RT at every T cell** |
| H3 | Killed-direction overlap vs R51-C3b | PASS (leading vs lagging distinction quantitative and well-argued) |
| H4 | Data sufficiency | PASS (31d, role-template 20d floor) |
| H5 | Latency feasibility | PASS (T=60s >> 210ms broker RTT) |
| H6 | Execution model | PASS (same QueueDepletionFill + v2026-04-24 profile as audit) |
| S1 | IC detrending | PASS (no IC used) |
| S2 | OOS validation | PASS (31d sample, jackknife policy) |
| S3 | Sample size | PASS (21 fills @ T=60s > floor 15) |
| S4 | Recency bias | PASS (31d recent) |
| **S5** | **Paper-to-code fidelity** | **FAIL — linear 0.2 pt/s slope is HYPOTHESIS, coefficient unvalidated** |
| S6 | Regime dependency | WARN (slope assumed stable across 31d; unverified) |
| **Survivor §1** | **Sign-robust AND positive** | **FAIL — V4 sign-robust NEGATIVE, not positive** |

Tier 1 FAIL: 1 (H2). Tier 2 FAIL: 1 (S5). Survivor §1: FAIL. Verdict: **REJECT**.

## α/β/γ trichotomy ruling (DA-confirmed)

**(β) DERIVES DISTINCT** confirmed. V4 operates at sp=5 baseline and does NOT touch the V1 sp=7 audit cell (+36.7 pt/fill, 9 fills, 1 winning day) that killed V1, V2, V3. This is the **first candidate in the run with a mechanistically distinct death-mode**. The cross-candidate H2 dependency chain is broken by V4.

## Three distinct kill modes now emerging (DA meta-finding for final summary)

| Axis | Round(s) | Kill mode |
|------|----------|-----------|
| A_spread | R1 V1, R2 V2 | Single-day dominance on inherited sp=7 audit cell → §3 jackknife fail |
| B_hedge | R3 V3 | Ratio inversion + cost arithmetic; hedge dormant at 31d batch horizons |
| C_queue | **R4 V4** | **Per-fill edge below retail cost floor at every cancel-T cell** |

All three axes now point at **retail cost (RT 4pt) vs TMFD6 median spread (4pt) = 100% cost drag** as the binding structural constraint. V4 is the cleanest case: mechanism provably works (halves V1's adverse) but cannot overcome cost ceiling.

## Lead observations

1. **DA correctly refused research-experiment override**. The /alpha-research run is explicitly "find deployable signals". V4 could be a valuable empirical measurement project (measure adverse-vs-lifetime slope across regimes), but that's a research task, not an alpha-promotion task. Logged as **data-collection follow-up** for final summary, not executed in-scope.
2. **Cross-candidate H2 chain BROKEN by V4**. This is a significant epistemic development. Previously all kills traced back to the same audit cell; V4 demonstrates that the kill mechanisms are axis-specific. If V5 also β-derives-distinct and also cost-floor-dies, the final finding becomes very clean: "three axes, three distinct kill modes, all binding on retail cost structure."
3. **Researcher self-diagnosed outcome correctly**. She estimated P=0.70 "cost-defeated negative" outcome; DA's kill-mode matches. This is the right epistemic shape — researcher's base rates are calibrated.
4. **S5 (linear-slope HYPOTHESIS) is a generalizable lesson**: any future variant that embeds a derived quantitative slope/coefficient into its T1 formula without T5-empirical validation should be flagged at T2. The temptation to extrapolate from V1 audit data is strong; DA's strict S5 reading is the right guard.

## Data-collection follow-up flagged (for final summary, not this run)

The empirical adverse-vs-lifetime slope measurement would require:
- Per-fill adverse_at_N_seconds column in audit reconstruction (not currently a hft-replay output)
- Per-side decomposition (buy-side P95=7s vs sell-side P95=32s lifetime asymmetry from audit)
- Cross-day regime stability check (31d panel, 0.2 pt/s slope invariance)

This is a multi-day data-engineering task. Legitimate follow-up if institutional-tier TMFD6 data becomes available OR if the R47 variant research resumes under different cost assumptions.

## Budget state

- Runtime used: ~1h of 24h (~4%)
- Rounds: 4/20
- Promotes: 0/3
- Consecutive KILLs: 4/8
- Pool remaining: V5 (V6-shelved, V1/V2/V3/V4 killed)

## Next round

R5 pops **V5 — L2 queue-depth-aware quote placement (Lipton-Pesavento-style)** (axis C_queue-position, priority 5, final candidate in pool). Expected structure: L2 depth filter at quote placement — thin queue = don't post, thick queue = post. Like V4, this is an axis-C variant operating on sp=5 base, which means:
- Should β-derives-distinct (doesn't touch sp=7 cell)
- Likely same cost-floor-structural kill mode as V4 (filtering fills doesn't raise per-fill edge above retail 4pt RT)

**Research-significant question for R5**: does V5 die the SAME way as V4 (validating the "retail cost ceiling" structural thesis), or a DIFFERENT way (which would weaken but not invalidate the thesis)?

If V5 KILLs on cost-floor → 5 consecutive kills, pool exhausted, run concludes with strong negative result. If V5 KILLs differently → richer final summary. If V5 surprise-APPROVEs → investigate carefully for any surviving signal.

DA pre-framing for R5 expected to include: "does L2 depth filter raise fills/day OR raise edge/fill? Mathematically it can only reduce fills (filter operation); edge/fill must therefore rise to compensate. Can a simple depth-thresholding do that?"

T1-R5-V5 dispatched to researcher.
