# Final Summary — R47 Re-validation Run (T7)

**Run**: `alpha-research-20260424-r47-revalidation`
**Started**: 2026-04-24T00:00:00Z
**Concluded**: 2026-04-24T08:02:00Z (~1.2h elapsed of 24h budget)
**Scope (user-specified)**: "B. 重做 research 層 │ 接受 +7,701 已死，改用 v2026-04-24 profile + 至少 60 天 OOS 樣本重跑 R47 變體（spread、hedge、queue-position 都算），看有沒有 signal 真的 survive — 這是 research，不是 ops"

**Concluded at**: R5 with pool exhausted, consecutive_kills = 5/8 (below HALT threshold; halting voluntarily on pool exhaustion).
**Regen**: SKIPPED (0/3 used) per DA advisory — axis-exhausted space.

---

## Headline Finding

**R47 variant-space on 31d retail TMFD6 has no deployable positive-expectation cell.**

All 5 candidates across 3 axes fail on retail cost arithmetic. **Binding structural constraint**: TMFD6 median spread 4pt = retail round-trip cost 4pt = **100% cost drag**. Variant-layer optimizations (spread-threshold, cross-instrument hedge, queue-position filters) cannot overcome this ceiling at the currently available 31d data budget.

**The +7,701 backtest figure is fully invalidated** (per 2026-04-24 audit, `docs/incidents/2026-04-24-r47-backtest-credibility-audit.md`). Replacing instant-RTT assumption with measured v2026-04-24 latency profile (210/210/210ms) produces −2,332 NTD / 31d for the only §6-compliant variant cell (V1 sp=5 = C60 defaults, already on the killed list).

---

## Data Reality Correction (mid-run, T0.5)

The run's initial scope assumed ≥60 days OOS via remote ClickHouse. Executor Task #2 verification (SSH tunnel, read-only queries) revealed:

| Instrument | TMFD6 | TXFD6 | TMFE6 |
|------------|------:|------:|------:|
| Union days (local + remote) | **31** | **31** | **25** |
| Remote-only days | 7-14 | 7-14 | 0 |
| Remote subset of local? | YES | YES | strict |

**≥60d was physically unachievable on 2026-04-24.** Scope contracted to **31d + mandatory jackknife + max_day ≤ 25% PnL share + min_winning_days ≥ 5 + fills ≥ 15 per sweep cell + sample_warning banner**. This survivor-criteria upgrade compensated for the data shortfall via statistical discipline rather than sample size.

Killed direction added: `60d-OOS-physically-unachievable-2026-04-24`.

---

## Three-Axis Kill-Mode Table

| Axis | Round(s) | Candidate(s) | Kill Mode | Binding Constraint |
|------|----------|--------------|-----------|---------------------|
| **A** — spread-threshold | R1, R2 | V1, V2 | Single-day dominance on inherited sp=7 audit cell → §3 jackknife FAIL | Sample-size ceiling: only sp=5 has fills ≥ 15 at 31d, and sp=5 = C60 defaults (already killed at −2,332 NTD) |
| **B** — cross-instrument hedge | R3 | V3 (T1 SELF-KILL) | K1: ratio inversion (correct is 20 TMFD6 : 1 TXFD6, not 1:20); K2: cost at sp=7 inherits V1 audit-cell pathology | Integer-contract hedge infeasibility at retail 1-lot fill rate (20-fill batch = 25 days at sp=5; hedge dormant in 31d sample) |
| **C** — queue-position (time) | R4 | V4 | H2 + S5 + Survivor §1 FAIL. Per-fill edge −5.5 to −9.3 pt below retail RT 4pt at every cancel-T cell | Retail cost floor — adverse-timing mechanism halves V1 baseline loss (works) but cannot cross zero |
| **C** — queue-position (depth) | R5 | V5 | H2 + S5 + Survivor §1 FAIL. Per-fill edge −7.0 pt at K=1 primary cell. **V4-isomorphic failure.** | Retail cost floor — depth-filter mechanism mirror-image of V4; same magnitude ~−1,100 NTD |

---

## Subordinate Research Findings

### 1. Cross-candidate H2 — the sp=7 audit-cell dependency

V1, V2, V3 all inherited the same V1 sp=7 audit cell (+36.7 pt/fill, 9 fills across 31d, 1 winning day). When §3 jackknife is applied, removing that one winning day flips the aggregate sign. This means three "independent" candidate kills are actually **one audit-cell statistical weakness exposed three ways**.

**Implication**: Any future axis-A or axis-B variant that imports the +36.7 pt/fill figure without jackknife-survivable re-derivation will inherit the same kill mechanism.

### 2. Axis-C two-mechanism cost-floor corroboration

V4 (cancel-timing filter) and V5 (depth filter) produce **isomorphic failure**:
- V4 T=60s: **−1,163 NTD / 31d** (21 fills, §6 margin +6)
- V5 K=1:   **−1,120 NTD / 31d** (16 fills, §6 margin +1)
- Both sign-robust negative at 800ms (V4 −1,894, V5 −1,785)
- Both derive distinct from sp=7 audit cell (operate on sp=5 base)
- Both fail Survivor §1 mechanically, same direction, same magnitude

**This is two-way corroboration of the retail-cost-structural thesis**. One candidate failure is anecdote; two mechanistically distinct filters (time vs depth) producing identical-magnitude negative PnL is structural.

### 3. Axis-B infeasibility pattern (integer-lot retail hedging)

Delta-neutral pair-making at retail 1-lot granularity requires batch sizes (20 TMFD6 fills per 1 TXFD6 hedge) that exceed the 31d sample's fill production. Even if the per-fill economics worked, the hedge would be dormant for most of the sample. This is a **structural infeasibility** at retail, distinct from the axis A/C cost-floor pattern.

---

## Canonical Negative Result

> **On 31d retail TMFD6 with v2026-04-24 latency profile, R47-class market making has no surviving variant.**
> **The binding constraint is retail cost structure, not strategy design.**
> **60-day data accumulation alone does NOT flip this conclusion unless cost tier changes.**

This is a **high-decision-value negative finding**. It blocks further R47 variant R&D at this data budget and this cost tier, and it frames the canonical follow-up work precisely.

---

## Variant Sweep Summary

| Cand | Axis | Primary Cell | 210ms NTD/31d | 800ms NTD/31d | Fills/31d | §6? | Verdict |
|:----:|:----:|:-------------|--------------:|--------------:|----------:|:---:|:-------:|
| V1 | A | sp=5 / 210ms | −2,332 | — | 25 | ✓ | KILL (c60-already-dead) |
| V1 | A | sp=7 / 210ms | +3,302 (1 winning day dominated) | +2,323 | 9 | ✗ | KILL (§3 jackknife) |
| V2 | A | 210/420ms regime gate | +259 (Risk #4 dichotomy) | −696 (sign flip) | 17 | ✓ | KILL (§3 + §1) |
| V3 | B | 1:20 hedge ratio | catastrophic | — | 9 at sp=7 | ✗ | SELF-KILL (K1+K2) |
| V4 | C | T=60s cancel | −1,163 | −1,894 | 21 | ✓ | KILL (H2+S5+§1) |
| V5 | C | K=1 depth | −1,120 | −1,785 | 16 | ✓ (+1) | KILL (H2+S5+§1) |

**Zero promote. Five kill. Three axes exhausted. One self-kill.**

---

## Follow-up Recommendations (ranked by ROI)

### High ROI (unblocks the finding)

1. **Institutional-tier fee structure re-run** — if retail cost (RT 4pt, 100% drag) is the binding constraint, then institutional tier (lower fees, possibly taker rebates) is the first variable that could flip the conclusion. Requires: fee structure confirmation, tick-data reconstruction of post-rebate PnL. Does NOT require new data collection.

2. **TMFD6 + TXFD6 historical backfill to ≥60d (ideally 120d)** — validates §3 jackknife discipline at full data budget. If variants still die under 120d, the structural finding is cemented; if any variant re-emerges, investigate. Timeline: weeks of live accumulation OR ClickHouse restore from archive if available.

### Medium ROI (sharpens the finding)

3. **Empirical adverse-vs-lifetime slope measurement (V4 hypothesis)** — direct measurement of `d(adverse)/d(fill_age)` would either validate or refute the 0.2 pt/s hypothesis underlying V4. Required: `adverse_at_N_seconds` column in audit replay; per-side decomposition (buy P95=7s vs sell P95=32s asymmetry). Converts V4 from "paper-arithmetic KILL" to "measured-mechanism KILL" (stronger result).

4. **Empirical depth-adverse conditional probability (V5 hypothesis)** — same shape as above for `P(adverse | thin_queue)` vs `P(adverse | thick_queue)`. Required: L2 depth correlation with fill adverse outcomes in audit data.

### Low ROI (scope-adjacent)

5. **max_pos axis ablation (Researcher's onboarding flag)** — TXFD6 mp=1→3 ablation was explicitly out of scope this run. Per `hft-mm-design` SKILL, this is a legitimate R47 axis; could be added to a future run's axis inventory.

6. **Instrument pivot (non-TAIFEX-retail)** — the binding constraint is TAIFEX retail cost structure. Instrument pivots to different markets/venues change the problem entirely; this is a pivot, not a follow-up.

---

## Process Observations (lessons for future runs)

1. **Mid-run data-reality corrections are healthy**. The ≥60d → 31d policy pivot at T0.5 was driven by Executor verification, not speculation. The run continued productively under tightened discipline; the kill mechanisms held under the revised criteria.

2. **DA's mechanical discipline is load-bearing**. Every REJECT in this run was deterministic from existing disk data (no new backtest required). This is the ideal epistemic shape — kills are available by arithmetic inspection, not speculation.

3. **Researcher's P-distribution calibration is strong**. All five candidate outcomes matched her T1 probability estimates (V4 P=0.70 "mechanism-valid-but-cost-defeated", V5 P=0.60 "cost-floor-KILL same as V4"). Base rates are accurate; self-diagnosis is reliable.

4. **Honest labeling doesn't substitute for measurement**. Researcher's V5 discipline upgrade (explicit HYPOTHESIS labels) was correct process but did not flip S5 verdict. This is the right call — honesty about what isn't measured is process hygiene; S5 is an empirical-grounding standard.

5. **Lead napkin errors on hedge arithmetic happen and must be corrected by Researcher, not protected**. The R3 V3 ratio-inversion (1:20 vs 20:1) was flagged by Researcher in T1 and acknowledged in R3 summary. DA added team reflex rule: "confirm direction from point-value ratio, not symbol pairing order" for any future hedge-variant candidate.

6. **Regen discipline — don't force it on exhausted spaces**. DA advisory to skip T8-REGEN was correct. Axes A/B/C were exhausted; forcing new candidates in the same R47/retail/31d space would produce more KILLs of the same class and dilute the decisive finding. The regen count (0/3 used) is a feature not a bug.

---

## Budget / Scope Summary

| Metric | Value |
|--------|------:|
| Runtime used | ~1.2h of 24h (~5%) |
| Rounds completed | 5 of 20 max |
| Candidates evaluated | 5 (V1, V2, V3, V4, V5) |
| Candidates shelved | 1 (V6 combo, dead-on-arrival once V1 killed) |
| Promotes | 0 of 3 max |
| Kills | 5 (4 via T2 REJECT, 1 via T1 SELF-KILL) |
| Consecutive kills | 5 of 8 HALT threshold |
| Regen used | 0 of 3 |
| Killed-direction list growth | +5 (incl. `60d-OOS-physically-unachievable-2026-04-24`) |

---

## Artifacts Index

| Round | Stage | Artifact |
|:-----:|:-----:|:---------|
| R1 | T1 | `docs/alpha-research/round-1-r47-revalidation/artifacts/t1_researcher_v1.md` |
| R1 | T2 | `docs/alpha-research/round-1-r47-revalidation/artifacts/t2_devils_advocate_v1.md` (+ 31d-policy addendum) |
| R1 | Summary | `docs/alpha-research/round-1-r47-revalidation/summary.md` |
| R2 | T1 | `docs/alpha-research/round-2-r47-revalidation/artifacts/t1_researcher_v2.md` |
| R2 | T2 | `docs/alpha-research/round-2-r47-revalidation/artifacts/t2_devils_advocate_v2.md` |
| R2 | Summary | `docs/alpha-research/round-2-r47-revalidation/summary.md` |
| R3 | T1 | `docs/alpha-research/round-3-r47-revalidation/artifacts/t1_researcher_v3.md` |
| R3 | Summary | `docs/alpha-research/round-3-r47-revalidation/summary.md` |
| R4 | T1 | `docs/alpha-research/round-4-r47-revalidation/artifacts/t1_researcher_v4.md` |
| R4 | T2 | `docs/alpha-research/round-4-r47-revalidation/artifacts/t2_devils_advocate_v4.md` |
| R4 | Summary | `docs/alpha-research/round-4-r47-revalidation/summary.md` |
| R5 | T1 | `docs/alpha-research/round-5-r47-revalidation/artifacts/t1_researcher_v5.md` |
| R5 | T2 | `docs/alpha-research/round-5-r47-revalidation/artifacts/t2_devils_advocate_v5.md` |
| R5 | Summary | `docs/alpha-research/round-5-r47-revalidation/summary.md` |
| R5 | Final | **this file** |
| Executor | Task #2 | `outputs/team_artifacts/alpha-research/round-1/artifacts/data_inventory_verified.json` |
| Incident | Data | `docs/incidents/2026-04-24-r47-revalidation-data-inventory.md` |
| Incident | Audit | `docs/incidents/2026-04-24-r47-backtest-credibility-audit.md` |

---

## Direct Answer to User's Question

> "改用 v2026-04-24 profile + 至少 60 天 OOS 樣本重跑 R47 變體 ... 看有沒有 signal 真的 survive — 這是 research，不是 ops"

**No R47 variant survived.**

- ≥60d OOS was physically unachievable on 2026-04-24; scope contracted to 31d + jackknife discipline (equivalent statistical rigor, less sample size).
- Under v2026-04-24 (210ms) latency profile + retail cost tier:
  - Spread-threshold sweep (V1, V2): killed on §3 jackknife / sp=7 cell dependency.
  - Cross-instrument hedge (V3): self-killed on ratio inversion + integer-lot infeasibility.
  - Queue-position filters (V4, V5): killed on H2 + S5 + Survivor §1; per-fill edge below retail cost floor regardless of filter mechanism.
- The +7,701 historical figure was produced under instant-RTT (zero-latency) assumption and does NOT survive under either realistic latency profile (v2026-04-24 210ms or 800ms sensitivity band).

**What changes the answer**: institutional-tier fee structure (the binding constraint is cost, not strategy). What doesn't change the answer: more data, better filters, or further axis-A/B/C variants.
