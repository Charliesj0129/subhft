# Round 5 Summary — V5 L2 Queue-depth-aware Quote Placement (Lipton-Pesavento-style)

**Run**: `alpha-research-20260424-r47-revalidation`
**Round**: 5 (final pool candidate)
**Candidate**: V5 — L2 queue-depth-aware quote placement (axis C_queue-position, priority 5)
**Instrument**: TMFD6
**Verdict**: **KILL** (T2 REJECT, H2 + S5 + Survivor §1 FAIL — identical pattern to V4)
**Dates**: 2026-04-24 T0..T2
**Consecutive kills after this round**: 5

## Pipeline stages

| Stage | Owner | Duration | Verdict |
|-------|-------|----------|---------|
| T1 | researcher | ~6 min | **PROCEED** (trichotomy (β), V4-identical death-mode predicted; PROCEED chosen for corroboration value, not survival odds) |
| T2 | devils-advocate | ~4 min | **REJECT — H2 + S5 + Survivor §1 FAIL** (same mechanical pattern as V4) |

## Artifacts

- `docs/alpha-research/round-5-r47-revalidation/artifacts/t1_researcher_v5.md`
- `docs/alpha-research/round-5-r47-revalidation/artifacts/t2_devils_advocate_v5.md`

## What V5 proposed

L2 depth filter at quote placement: if queue at our price level has `depth < K` contracts ahead of us, DO NOT post; wait until depth thickens to ≥ K. Rationale: thin queue correlates with informed flow (higher toxicity); thick queue = anonymous participation. Sweep K ∈ {1, 2, 3, 5}.

## Researcher T1 arithmetic (31d, sp=5 base, sweep K)

| K | fills/31d | NTD/31d @ 210ms | NTD/31d @ 800ms | §6 (≥15)? |
|---:|---:|---:|---:|---|
| **1** | **16** | **−1,120** | **−1,785** | PASS (+1, margin fragile) |
| 2 | ~12 | — | — | **FAIL** (<15) |
| 3 | ~8 | — | — | FAIL |
| 5 | ~4 | — | — | FAIL |

V5 is **sign-robust NEGATIVE** at both latencies (magnitude worsens 59% at 800ms — same pattern as V4's 63%).

## Researcher's direct answer to the central question

> **Q: Is V5's death-mode identical to V4's, or is there a genuinely distinct mechanism?**
> **A: IDENTICAL.** V4 and V5 are mechanistically isomorphic strategy-layer adverse-selection filters at sp=5 base under retail 4pt RT cost floor. Only the filter variable differs (depth-at-post vs age-at-fill). P=0.60 cost-floor KILL at −800 to −1,500, P=0.20 worse than V4.

She chose **PROCEED (not SELF-KILL)** explicitly for **corroboration value** — running V5 and observing V4-identical death-mode corroborates the retail-cost-ceiling thesis with two independent axis-C mechanisms, which is epistemically stronger than V4 alone.

## Kill reasoning (DA T2)

Three independent mechanical fails, identical pattern to V4:

### 1. Survivor §1 direct-text FAIL

V5 K=1 at 210ms = −1,120 NTD. Fails `pnl_positive_at_210ms: true` conjunct (a). Sign-robust-negative ≠ §1 survivor. Mechanical from Researcher's projection.

### 2. H2 FAIL — edge below cost floor

Per-fill edge = −1,120 / 16 = −70 NTD/fill = −7 pt/fill, below retail 4pt RT cost and below role-template 8pt threshold. Every K cell negative; V5 does not cross zero.

### 3. S5 FAIL — unmeasured conditional-probability mechanism

Key hypothesis `adverse(thin) − adverse(thick) ≈ 7 pt` is the entire mechanism driver. Unmeasured at T1. DA rules S5-equivalent-class to V4's slope coefficient: both are unmeasured mechanism drivers; different mathematical form (conditional probability vs linear slope), equivalent S5 violation class.

**Key process note from DA**: Researcher's explicit HYPOTHESIS labeling this round (discipline upgrade per V4 lesson) is noted as process improvement but **does NOT flip the S5 verdict** — role-template S5 is about empirical grounding, not labeling honesty. This is correct: honesty about lack of measurement doesn't substitute for measurement.

## H/S scoreline (T2 Kill Checklist)

| Tier | Check | Verdict |
|------|-------|---------|
| S0 | Scope | PASS |
| H1 | Cost arithmetic | PASS |
| **H2** | **Spread vs edge** | **FAIL — −7.0 pt/fill below 8pt role-template threshold** |
| H3 | Killed-direction overlap (R5 `queue-position-gating-layer-on-c60`) | PASS (re-admit clause authorized via trigger-variable distinction, not satisfied — T3 empirical would be required for satisfaction) |
| H4 | Data sufficiency | PASS |
| H5 | Latency feasibility | PASS |
| H6 | Execution model | PASS |
| S1 | IC detrending | PASS |
| S2 | OOS validation | PASS |
| S3 | Sample size | MARGINAL PASS (K=1 = 16 fills, margin +1 only) |
| S4 | Recency bias | PASS |
| **S5** | **Paper-to-code fidelity** | **FAIL — `adverse(thin)−adverse(thick)≈7pt` is unmeasured hypothesis** |
| S6 | Regime dependency | WARN (thin/thick partition assumed stable across 31d) |
| **Survivor §1** | **Sign-robust AND positive** | **FAIL — V5 sign-robust NEGATIVE, not positive** |
| Survivor §6 | Fills ≥ 15 | MARGINAL PASS (+1 margin only; narrowest of any run candidate) |

Tier 1 FAIL: 1 (H2). Tier 2 FAIL: 1 (S5). Survivor §1: FAIL. Verdict: **REJECT**.

## α/β/γ trichotomy ruling

**(β) DERIVES DISTINCT** confirmed. V5 operates at sp=5 base (same escape from sp=7 audit cell as V4). Two-for-two β-confirmed axis-C candidates both die on cost-floor — the axis-C mechanism **does work** (reduces adverse) but **cannot cross the retail cost ceiling**.

## Cross-candidate meta-finding (complete, ready for final summary)

Three-axis kill-mode table now finalized:

| Axis | Round(s) | Candidate(s) | Kill mode |
|------|----------|--------------|-----------|
| A (spread-threshold) | R1, R2 | V1, V2 | Single-day dominance on sp=7 audit-cell inheritance → §3 jackknife FAIL |
| B (cross-instrument hedge) | R3 | V3 (SELF-KILL) | Ratio inversion + hedge cost 600× maker gross at retail RT |
| C (queue-position, timing) | R4 | V4 | Per-fill edge below retail cost floor at every T cell |
| C (queue-position, depth) | R5 | V5 | Per-fill edge below retail cost floor at every K cell (identical to V4) |

**Canonical finding**: All three axes fail on **retail cost arithmetic**. Binding structural constraint: TMFD6 median spread 4pt = retail RT 4pt = 100% cost drag. No variant-layer optimization crosses this ceiling at 31d data budget.

## Lead observations

1. **Researcher's honest P=0.60 KILL call materialized**. She predicted V5 would cost-floor-die same as V4; DA REJECTed on identical mechanical pattern. Calibrated base rates.
2. **V4 + V5 isomorphism is the run's cleanest technical result**: two mechanistically distinct filters (time vs depth) both produce ~−1,100 NTD / 31d and both fail §1 in the same direction. This is two-way corroboration of the retail-cost-structural finding, not one-candidate noise.
3. **DA's "process discipline doesn't flip empirical verdict" ruling is correct**. Explicit HYPOTHESIS labeling (researcher's V5 discipline upgrade) is exactly the right process shape but cannot substitute for measurement. The T2 standard is empirical grounding — paper-arithmetic remains paper-arithmetic no matter how honestly labeled.
4. **Regen skip decision**: DA advised skipping T8 regen; agreed. Axes A/B/C are exhausted. Regen candidates would either (a) restate a dead axis = waste, or (b) require instrument pivot = scope violation. Forcing regen on exhausted space would dilute the decisive finding into noise.

## Budget state

- Runtime used: ~1.2h of 24h (~5%)
- Rounds: 5/20
- Promotes: 0/3
- Consecutive KILLs: 5/8 (below HALT threshold 8; halting voluntarily on pool exhaustion)
- Pool remaining: EMPTY (V6 shelved per prior)
- Regen used: 0/3 (DECISION: SKIP per DA advisory)

## Next action

**Proceed to T7 final summary** (`docs/alpha-research/round-5-r47-revalidation/final_summary.md`). No further rounds.
