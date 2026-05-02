# Round 1 Summary — V1 Spread-threshold Structural Sweep

**Run**: `alpha-research-20260424-r47-revalidation`
**Round**: 1
**Candidate**: V1 — Spread-threshold structural sweep at 210ms (axis A_spread, priority 1)
**Instrument**: TMFD6
**Verdict**: **KILL** (T2 REJECT, S3 FAIL)
**Dates**: 2026-04-24 T0..T2
**Consecutive kills after this round**: 1

## Pipeline stages

| Stage | Owner | Duration | Verdict |
|-------|-------|----------|---------|
| T1 | researcher | ~7 min | PROCEED (narrow pre-research gate pass) |
| T2 | devils-advocate | ~4 min | **REJECT — S3 FAIL (sample-size)** |
| T2-addendum | devils-advocate | ~1 min | REJECT UNCHANGED + STRENGTHENED under revised 31d data policy |
| T4-T7 | (executor, lead) | SKIPPED | no revision loop per task chain |

## Artifacts

- `docs/alpha-research/round-1-r47-revalidation/artifacts/t1_researcher_v1.md` (patched mid-round with side-by-side 210/800ms arithmetic)
- `docs/alpha-research/round-1-r47-revalidation/artifacts/t2_devils_advocate_v1.md` (with 31d-policy addendum)
- `outputs/team_artifacts/alpha-research/round-1/artifacts/data_inventory_verified.json` (Executor Task #2)
- `docs/incidents/2026-04-24-r47-revalidation-data-inventory.md` (Executor Task #2 full report)

## Kill reasoning

V1 proposed sweeping `spread_threshold_pts ∈ {5, 6, 7, 8, 9, 10, 12, 15, 20}` under v2026-04-24 (210ms) + 800ms sensitivity against ≥60d OOS, with survivor criteria explicitly designed to rule out the `spread-threshold-7-single-outlier-day` kill entry.

Two independent mechanisms kill V1 under the actual 31d data budget:

1. **S3 sample-size collapse at favorable cells**. Researcher's own arithmetic: sp=7 produces 0.29 fills/day = 9 fills / 31d. Policy §3 (jackknife survive) fails mechanically — audit data already shows 1 winning day / 9 fills at sp=7, removing that day flips the sign. Policy §4 (max_day ≤ 25%) fails — 1 day is >100% of aggregate positive PnL. Policy §6 (fills ≥ 15) fails at sp=7 (9), sp=10 (5), sp=15 (1). Only sp=5 meets fills ≥ 15 at 31d (25 fills).
2. **Only policy-compliant cell is already killed**. sp=5 is C60 defaults which are blacklisted as `c60-r47-minimal-as-deploy-ready` at the audit-measured −2,332 NTD / Sharpe −2.98. So the only sp cell with enough fills to satisfy the sample-size policy is structurally dead; all other cells fail on fill counts.

Every cell in V1's grid violates at least one new-policy criterion from already-available audit data. The test was deterministic; 31d does not rescue the candidate, it accelerates the kill.

## H/S scoreline (T2 Kill Checklist)

| Tier | Check | Verdict |
|------|-------|---------|
| S0 | Scope | PASS (pure_maker in allowed_types; no forbidden rules tripped) |
| H1 | Cost arithmetic | PASS (retail RT=4pt verified) |
| H2 | Spread vs edge | PASS-with-bright-line-WARN (drag 100% baseline, 57% at sp=7, 40% at sp=10) |
| H3 | Killed-direction overlap | PASS (Researcher designed the re-admit conditions of `spread-threshold-7-single-outlier-day` by construction) |
| H4 | Data sufficiency | PASS unconditional (31d >> 20d role-template floor; prior shared-context 60d constraint relaxed per Lead data-policy pivot) |
| H5 | Latency feasibility | PASS (quote lifetime 32s vs 2×210ms=420ms → 76× margin) |
| H6 | Execution model | PASS (bid/ask-aware MakerEngine) |
| S1 | IC detrending | PASS (maker, no IC-horizon contamination) |
| S2 | OOS validation | PASS (60d-plan → 31d jackknife) |
| **S3** | **Sample size** | **FAIL — only sp=5 has fills ≥ 15 at 31d, and sp=5 is already killed** |
| S4 | Recency bias | PASS (31d is the most recent window, Mar-Apr 2026) |
| S5 | Paper-to-code fidelity | PASS (QueueDepletionFill(qf=0.5) + v2026-04-24 profile match audit exactly) |
| S6 | Regime dependency | OK at 31d (single regime, no Jan-Feb mixing needed) |
| P1-P3 | Platform compatibility | OK/WARN (no changes needed for this round) |

Tier 1 FAIL count: 0. Tier 2 FAIL count: 1 (S3). Verdict: **REJECT**.

## Cross-candidate warning for future rounds (from DA T2 addendum)

The 31d data budget structurally constrains ANY candidate whose favorable cell has fills/day ≤ 0.5 from satisfying policy §3 AND §5 AND §6 simultaneously. This likely affects:
- V3 (TXF-TMF 1:20 hedge) — fill rate is on TMFD6 maker side = same sparsity population
- V4 (queue-age cancel) — cancels MORE aggressively = fewer fills
- V5 (L2 thin-queue filter) — filters fills = fewer fills
- V6 (V1 + V4 combo) — dependency chain on V1 and V4 survival

V2 (adaptive spread gate conditional on quote-lifetime regime) is the one candidate where fill-rate mitigation might differ — the regime-adaptive mechanism could, in principle, trigger fills in a distinct selection regime. Researcher for R2-V2 should address this as Risk #1 in her T1.

**The test to watch**: does any candidate raise effective TMFD6 fills/day above the 0.5 threshold while keeping edge positive? If none do, the structural finding of this run is "R47 variants on TMFD6 retail 31d are all sparse-fill constrained; 60d data collection is prerequisite for any deploy-significance evidence."

## Discipline rules added this round

- **DA-added S5 fill-model continuity rule** (2026-04-24T07:12): every T5 backtest must use `QueueDepletionFill(qf=0.5)` + `r47_maker_shioaji_p95_v2026-04-24` matching audit reference, or document deviation with dual-report cross-comparison. Also: CK-direct ground truth recommended per 14x bias rule (WARN-only if missing).
- **DA-added H4/S3 gating** (2026-04-24T07:21): future rounds gate re-admit of any sample-size-failed candidate on `data_inventory.ge_60d_available` flag flipping to true. Current value is `false` on all three instruments.
- **Policy §7 banner extension** (2026-04-24T07:21): `sample_warning: small_sample_31d_panel` banner extends from scorecards to T2 verdicts as well.

## Lead observations (Team Lead)

1. DA's T2 addendum is structurally important — the REJECT verdict under the revised 31d policy is STRONGER, not weaker, because jackknife fails mechanically on existing audit data without needing a new backtest run. This is the ideal epistemic shape for a research round: the answer was deterministically available on disk.
2. Max_pos axis gap (Researcher's onboarding flag) remains deferred. Any TXFD6 mp=1→3 ablation is a legitimate R47 axis but is NOT in this run's scope per user's explicit "spread / hedge / queue-position" framing.
3. Data-collection recommendation for follow-up run: TMFD6 + TXFD6 historical backfill to ≥60d (ideally ≥120d covering 2026-Q1 + post-rollover 2026-Q2) OR live accumulation of new observation days over the coming weeks until union reaches ≥60d.

## Next round

R2 pops **V2 — Adaptive spread gate conditional on quote-lifetime regime** (axis A_spread, priority 2). T1-R2-V2 dispatched to researcher.
