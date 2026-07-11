# Round R6 Summary — C32b_tob_survival_refresh_regime_gate_rescue (fresh-run 2026-04-18)

**NOTE**: Prior-halted-run R6 (C14 PROMOTE-then-REVOKED) archived before this fresh run. This file replaces the prior summary.

- **Round ID**: R6
- **Candidate**: C32b_tob_survival_refresh_regime_gate_rescue (T2-evidence-rescue of R2 C32)
- **Type**: exec_support_signal (R47 refresh-cadence modulator)
- **Instrument**: TMFD6 day session
- **Start**: 2026-04-18T11:48Z
- **End**: 2026-04-18T12:28Z
- **Duration**: ~40min
- **Verdict**: **KILL** (DA Gate C REJECT at T6; T2 APPROVE revoked on formula-physics cascade — R5 C30-class pattern)

## Stages traversed

- **T0** (Lead): C32b popped from T8-REGEN-1 pool (7 survivors; Researcher priority #1)
- **T1** (Researcher): PROPOSE with pre-closed Tier-2 evidence. IS-honest threshold sweep selected 200ms on IS; OOS applied unchanged showed 100% sign-agreement + 79% retention. S3: 22,391 independent trades under spread≥5 filter. Apr 10 fill-sim: +5.8% expected fills (**UNCAPPED formula — seed of later failure**).
- **T2** (DA): **APPROVE** (0/0 Tier 1/2 FAIL, 5 WARNs). Audited IS-honesty, trade-count integrity, no-order-actions physics — but did NOT open fill-sim script to audit the formula itself.
- **T3**: SKIPPED
- **T4** (Executor): 27/27 tests PASS. Gate A structural PASS. Physics verified: no order methods on modulator.
- **T5** (Executor): **Thresholds FAIL**. Mean daily lift −32.30 NTD ALL / −40.21 NTD OOS. **0/12 days positive**. Central physics dispute: Researcher's uncapped `sum(trades/depth)` vs physically-correct `sum(min(1, trades/depth))` cap.
- **T6** (DA): **Gate C REJECT** — T2 APPROVE revoked. Capped formula ruled physically correct (single bounded-size quote fills AT MOST once per life).
- **T7** (Lead): KILL recorded.

## Kill reason — formula-physics cascade (R5 C30-class)

- Researcher T1: `expected_fills = sum(trades / max(queue_depth, 1))` — UNCAPPED → +5.8% Apr 10
- Executor T5 (physics-correct): `sum(min(1.0, trades / max(queue_depth, 1)))` — per-life cap → −4.57% Apr 10
- **Even Executor's uncapped reproduction** = −1.30% on Apr 10 (vs Researcher's +5.8%). 7.1pp gap unreconciled at code level but settled on physics

**Three convergent failure modes**, each individually sufficient for REJECT:
1. **Formula physics**: T1 evidence invalid under capped formula
2. **Realized PnL**: 0/12 days positive; p=3e-5 ALL in NEG direction (formula-independent)
3. **Mechanism doesn't convert**: reset reduction confirmed (16% ALL / 24% OOS) but queue-priority retention does NOT translate to more realized fills — non-mid-moving events rebuild queue competitors

## DA's self-critique

> "My T2 APPROVE was vulnerable to this failure. I verified IS-honest selection, trade-count integrity, incremental-cost physics ('no new place/cancel' audit), but did NOT audit the fill-sim formula. The pattern is identical to R5: DA T2 accepts a Researcher-provided arithmetic headline containing a physics omission; Executor catches it at T5; T6 revokes T2. Both times, DA applied physics verification to one side of the claim (cost) but not the other (R5: hedge-qty; R6: fill-probability)."

## Process finding (team discipline worked 3× in a row)

R5 C30, R6 C32b, and prior-run R6 C14 are the three instances of the "DA T2 APPROVE revoked at T6 on Executor-caught physics error" pattern. Team structure caught 3/3 pre-PROMOTE. Executor as implementer has lowest tolerance for formula/physics shortcuts.

## New meta-finding (DA's H1-physics role-contract proposal)

> **H1-physics (formula audit rule)**: For any quantitative claim Researcher supplies at T1 (cost, fill-probability, edge-per-fill, queue-priority-value, hedge-qty, latency-survival), DA MUST trace the formula back to a physical invariant and verify the formula respects that invariant. Specifically:
> - Cost: cost = qty × rate; qty from notional-matching (R5 rule)
> - Fill-probability: single bounded-size quote → AT MOST one fill per life; demand min(1, trades/depth) cap (R6 rule)
> - Queue-priority value: theoretical lift ≠ realized lift; empirical demonstration required
> - Edge per fill: verify against actual spread at fills
> - Latency-survival: half-life ≥ 2× RTT

## Artifacts

All under `outputs/team_artifacts/alpha-research/round-6/artifacts/`:
- `researcher_t1_proposal.md`, `researcher_t1_counterfactual_result.md`, `researcher_t1_data_inventory.md`
- `explore_c32b_is_oos.py` + `c32b_is_oos_partition.json`
- `c32b_apr10_fill_sim.py` (uncapped formula at line 174 — evidence artifact) + `c32b_apr10_fill_sim_result.json`
- `da_t2_kill_checklist.md` (APPROVE, 5 WARNs — later revoked)
- `c32b_t5_backtest.py`, per-day/per-cycle/agg CSVs
- `executor_t5_scorecard.md`, `executor_t5_bracket_sweep.md`
- `da_t6_gate_c.md` (Gate C REJECT, 203 lines)

Implementation: `research/alphas/c32b_tob_survival_refresh_regime_gate/` (package retained for reference).

## Killed_directions append

```yaml
- id: "tob-survival-refresh-regime-gate-queue-priority-no-convert"
  rounds: "R6-fresh"
  reason: "C32b Gate C REJECT at T6. T2 APPROVE revoked on formula-physics: Researcher T1 used uncapped sum(trades/depth); Executor T5 applied physics-correct min(1, trades/depth) cap. Apr 10 uncapped +5.8% → capped −4.57%. Mean daily lift empirically −32.30 NTD ALL / −40.21 NTD OOS; 0/12 days positive. Mechanism correctly delays refreshes (16-24% reset reduction) but queue-priority retention does NOT translate to realized fills — non-mid-moving events rebuild queue competitors. Any future C32-class regime-gated refresh-modulator on TMFD6 max_pos=1 deploy is in this killed class."
```

## Lead Follow-Ups (accumulated for final-summary)

1. **DA role template H1-physics subcheck** (R6 proposal; extends R5 hedge-qty-verification)
2. **Researcher role template formula-audit requirement**: T1 fill-sim/edge-model formulas must be annotated with physics derivation
3. R5 carry-overs still open (hedge-qty sidebar, hedge-qty verification, TXO scale, parity-rate template)

## Progress

- **Rounds completed**: 6 of 20
- **PROMOTEs**: 0 of 3
- **Consecutive KILLs**: **6 of 8** — **2 rounds runway left** before halt
- **Runtime used**: ~4h5m of 24h
- **Pool remaining**: 8 after C32b pop `[C33, C36, C35b, C33b, C39, C37, C34, C35]`
- **regen_count**: 1 of 3

## Next (R7)

Pop **C33_txfd6_solo_passive_maker** (regen-1 priority #2).

**Why C33 may survive**: TXFD6 cost profile is structurally better than TMFD6 (75% drag vs 200%). Pure R47 maker; no cross-instrument entanglement; no fill-sim formula required (straightforward fill-level backtest).

**Pre-flight warnings for R7/T1** (applying R5+R6 lessons upfront):
- Fill-probability physics rule (NEW): any expected-fill diagnostic must use per-life-bounded cap
- R47 calibration transfer: PE/Queue/MFG thresholds calibrated on TMFD6; T1 must test R47-full AND R47-minimal on TXFD6
- Regime persistence: auto-disable if TXF sp_med<3 for 3 consec days
- Data fidelity: only 4-7 high-fid days Apr 3+ for TXFD6; IS/OOS tight
- Cost citation: `memory/feedback_taifex_fee_structure.md` for TXF RT=3pt retail

---

## Prior-halted-run (2026-04-17 C14 PROMOTE-REVOKED) archived below for reference only

- **Round ID**: R6 (prior halted run; archived)
- **Candidate**: C14_txf_frontmonth_native_maker (scope amendment from C14_txfd6_native_maker at T1)
- **Type**: pure_maker (R47 strategy on TXF rolling front-month)
- **Instrument**: TXF rolling front-month (TXFB6 → TXFC6 → TXFD6)
- **Start**: 2026-04-17T11:58Z
- **End**: 2026-04-17T13:35Z
- **Verdict**: ~~PROMOTE (first PROMOTE of the run)~~ **REVOKED → KILL** (2026-04-18 correction)

## Stages

- **T1** Researcher: `round-6/artifacts/t1_researcher_proposal.md` (20 KB). Fixed-TXFD6 parity FAILED 98.1% (contract-rotation artifact). Researcher pivoted to rolling front-month; spread stable 2-5 pt year-round.
- **T2** Devil's Advocate: APPROVE (0/0/0 Tier 1/2/3 FAILs, P2 WARN on rollover engineering). Scope amendment NOT judged a goalpost move.
- **T4** Executor: `research/alphas/c14_txf_frontmonth_native_maker/` (impl.py + frontmonth.py + manifest + README + 26 tests). R47 composed via import, unmodified. Rollover: volume-crossover + calendar fallback.
- **T5 initial**: OOS 12d Sharpe 8.22, +100K NTD/day at 40% scalar discount.
- **T6 initial Gate C**: CONDITIONAL. 2 unrescued fails — scalar discount directionally biased; OOS TXFD6-only with zero rollovers.
- **T5-REVISE**: QueuePositionStochasticFill (impl.py:462-552) + OOS widened to 20d spanning TXFC6→TXFD6 rollover. 33/33 tests.
- **T6-REVISE Gate C**: **PASS**. Both prior fails rescued. 2 WATCH items, 6 PASS.

## Headline (p_front=0.3, primary)

- OVERALL Sharpe 11.55, +9.78M NTD over 40 days.
- **OOS 20d (2026-03-13 to 04-14): Sharpe 18.89, +312.9K NTD/day, 19/20 winning days**.
- Edge/RT 1.73× OOS (above 1× floor).
- Per-contract OOS: TXFC6 +320.8K/day (4d), TXFD6 +310.9K/day (16d) — 3% cross-contract diff validates rolling-front-month thesis.
- Rollover cost: -19.2 pts (2 events), credible.

## Sensitivity (OOS NTD/day)

- p_front=0.2 (pessimistic): +262K
- p_front=0.3 (default): +313K
- p_front=0.5 (optimistic): +378K

All sign-stable, monotone. Edge/fill also monotone (0.546/0.722/0.841 pts).

## Statistical Robustness

- Bootstrap CI on OOS Sharpe: [9.4, 27.4] annualized.
- P(19/20 winning days | H0 Sharpe=3) = 0.03%. Very robust.

## WATCH Items (non-blocking, flagged for Gate E shadow validation)

1. **T1-parity gap**: OOS +313K NTD/day is 7× Researcher's T1 best case +45K. Root cause: queue-position model retains +62% more volume than scalar-discount baseline. Structural CK-direct-to-retail fidelity gap, not fixable at fill-model level. Must be validated against live shadow execution.
2. **Sharpe 18.89 lower-CI 9.4 still high**: real live trading will likely produce lower Sharpe. Shadow must size conservatively (p_front=0.2 pessimistic per Challenger recommendation).

## Lead T7 Decision: PROMOTE

All formal gates cleared. Statistical robustness validated. OOS validates regime-escape thesis across contract rotation. Challenger recommendation: size shadow on p_front=0.2 pessimistic.

Alpha Governance status:
- Gate A (manifest): DONE at T4
- Gate B (tests): DONE (33/33)
- Gate C (backtest): DONE at T6-REVISE (PASS)
- Gate D (Sharpe/DD): PASS (CI lower 9.4 ≫ typical 1.5 threshold)
- Gate E (shadow session): **POST-ROUND RESPONSIBILITY** — to be scaffolded at T8

## T8 Post-PROMOTE Actions

1. **Scaffold**: task #20 to Executor — write BaseStrategy wrapper (impl.py already AlphaProtocol-conformant; BaseStrategy wrapper needed for runtime pipeline).
2. **Config entries**: `strategies.yaml` + `strategy_limits.yaml` for shadow (max_pos=3, p_front=0.2, daily-loss hard stop).
3. **Shadow scaffold**: `HFT_ORDER_SHADOW_MODE=1` integration; paper capital ~552K NTD (3× contract margin 184K).
4. **Release gate checklist**: per `hft-release-gate` skill — pre-live validation (user must confirm before live).
5. **Production audit plan**: per `hft-production-audit` skill — post-first-live sweep.

User confirmation required before shadow→live (per feedback memory `feedback_no_auto_deploy.md`).

## Progress

- Rounds completed: 6 of 20
- PROMOTEs: **1 of 3** (first of run)
- Consecutive KILLs: **0** (reset by PROMOTE)
- Runtime used: ~5h20m of 24h
- regen_count: 1 (of 3)

## Process Observations

1. **Scope amendment was legitimate**: the original C14 sketch conflated "TXFD6" (specific contract) with "TXF front-month" (rolling instrument). Parity test caught it at T1; reframe preserved mechanism. Future TAIFEX-futures candidates need "front-month audit" before pool admission (final-summary follow-up).
2. **Revise loop at T6 worked**: Gate C CONDITIONAL with specific defects → targeted Executor revision → Gate C PASS. Precedent for future fidelity-not-hypothesis defects.
3. **Regime-escape axis (non-TMFD6 venue) delivered the first PROMOTE**. C14's success does NOT generalize to C15/C16/C18 (TMFD6 modulators). C17 (TMFB/TMFC off-expiry) is the closest parallel.

## Next round (R7)

Lead will pick R7 after T8 scaffolding completes. Default pop: **C17 (TMFB/TMFC off-expiry)** as parallel validation of the regime-escape-via-venue thesis. Alternative: C16 (quote-age throttle, observed-state axis) if Lead wants a different axis after C14. Decision deferred to T9 after scaffold completes.

---

## Correction — 2026-04-18 (PROMOTE REVOKED → KILL)

**Trigger**: User reported that actual TXF retail RT ≈ 3 pt, not the 0.48 pt assumed in C14 manifest cost_model. Manifest cost_model has been corrected to `total_rt_pts: 3.0` per `feedback_taifex_fee_structure.md`.

### Re-computed OOS economics (RT 0.48 → 3.0)

| Metric | T5-REVISE (RT=0.48) | Corrected (RT=3.0) |
|--------|--------------------:|-------------------:|
| Net edge per fill | +0.829 pt | **−1.69 pt** |
| OOS 20d total | +31,288 pt | **−63,784 pt** |
| OOS NTD/day | +312,900 | **−638,000** |
| Edge/RT ratio | 1.73× | **0.44×** (under 1× floor) |
| H2 threshold (4.3 + 3.0 = 7.3 pt) | — | gross 1.31 pt → **FAIL by 5.6×** |
| Maker-unit (half-spread 1.31 vs 2×half_RT 3.0) | — | **FAIL by 2.3×** |
| Cost drag | 11% | **70%** |

### Why nobody caught it

| Role | Should have done | Actually did |
|------|------------------|--------------|
| Researcher T1 | Cite `feedback_taifex_fee_structure.md` for RT | Cited `r47_backtest_data_regression.md` (which itself may have wrong RT) |
| Challenger T2 H1 | Independently verify RT against memory | Verified Researcher's arithmetic but not RT base |
| Challenger T6 Gate C | Re-audit cost base in scorecard | Caught queue-fill bias + OOS scope, missed cost base |
| Lead T7 | All gates pass → PROMOTE | Did not re-audit upstream cost assumptions |

### Why this is consistent with R10 C17 KILL

R10 KILLED C17_tmf_frontmonth_native_maker on **STRUCTURAL BOUNDARY**: "R47 venue-change works where RT << spread (TXF 8-13× cushion) but fails where RT ≈ spread (TMF 0.75× cushion)". The boundary observation was correct, but the line was in the wrong place because TXF cushion was over-stated under wrong RT. With corrected RT=3 on TXF spread 4.3 pt, **TXF cushion = 1.43×, not 8-13×**. Both contracts cross the boundary; the meta-finding "TXF passes" was wrong.

### Cascade impact

1. **R14 C27_VOL_AMPLIFIED_C14**: amplifies C14's max_pos in high-vol windows. Since C14 base is structurally negative under correct RT, C27 amplifies the loss. R14 must be HALTed (in flight at T5).
2. **R47 backtest +4,504 pts/12d historical baseline** (cited in T1 §3.5): if computed with RT=0.48, also overstated. `r47_backtest_data_regression.md` needs re-verification.
3. **R10 C17 KILL stands**: KILL was correct, but the meta-finding "TXF passes" needs amendment to "BOTH TXF and TMF cross the cost boundary at retail".

### Killed-direction entry (appended to candidate_pool.json)

```yaml
- id: "txf-frontmonth-cost-structure-assumption-error"
  rounds: "R6"
  reason: "C14 PROMOTEd 2026-04-17 on assumed TXF RT=0.48 pt. User confirmed 2026-04-18 actual retail RT≈3 pt (6× under-estimate). Recomputed OOS: -638K NTD/day, edge/RT 0.44× (well under 1× floor), H2 FAIL by 5.6×, maker-unit FAIL by 2.3×. Cost drag 70% (close to TMFD6 80-133%, NOT the 10-16% claimed). The 'TXF cost-structure advantage' thesis is FALSIFIED. Future TAIFEX-futures candidates MUST cite feedback_taifex_fee_structure memory for RT or request user confirmation; never infer from research-side configs."
```

### Behavioral rules added

1. **Researcher T1 cost gate** (`.agent/teams/alpha-research/roles/researcher.md`): every proposal MUST cite `feedback_taifex_fee_structure` memory for RT, OR explicitly request user confirmation when memory is silent.
2. **DA H1 cost-source check** (`.agent/teams/alpha-research/roles/devils-advocate.md`): H1 (cost arithmetic) now requires DA to independently verify RT base against `feedback_taifex_fee_structure` memory.
3. **Cost drag in T2 verdict**: "Cost drag = RT / median_spread" must be reported in T2 H2 verdict; > 50% drag = bright-line WARN.

### Final R6 verdict

| Stage | Original | Corrected |
|-------|----------|-----------|
| T2 | APPROVE | INVALID (cost basis wrong) |
| T6-REVISE | PASS | INVALID (cost basis wrong) |
| **T7** | **PROMOTE** | **KILL** |
| T8 | scaffold complete | INVALID — `enabled: false` enforced; banners added to RELEASE_GATE/SHADOW_DEPLOY/manifest/strategies.yaml comment |

PROMOTE count: 1 → **0**. Consecutive KILLs reset clarification: R6 was incorrectly counted as PROMOTE; this correction does not retroactively change downstream rounds' consecutive-KILL counter (R7-R13 were KILLs; if R6 is also KILL, R6-R13 = 8 consecutive KILLs — at the budget-guard halt threshold).
