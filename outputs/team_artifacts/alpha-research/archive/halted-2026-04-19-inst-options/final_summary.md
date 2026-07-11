# Alpha-Research Run — Final Summary

**Run**: alpha-research-20260419-inst-options
**Started**: 2026-04-19T00:00Z
**Halted**: 2026-04-19T07:15Z
**Runtime used**: ~7h 15m of 24h budget (30%)
**Halt trigger**: `max_consecutive_kills_reached` (8/8 at end of R11)
**Halt quality**: clean structural-exhaustion signal (not premature, not regen-stuck)

## Aggregate verdict

| Metric | Count | of Budget |
|---|---:|---:|
| Rounds completed | 11 + R2-SUPP | of 20 |
| PROMOTEs | **2** | of 3 |
| KILLs | 9 | n/a |
| Consecutive KILLs at halt | 8 | of 8 |
| Regen invocations | 1 | of 3 |

## PROMOTED candidates (awaiting manual user shadow→live approval)

### 1. C60 TMFD6 R47-minimal (R1 PROMOTE_CONDITIONAL)

- **Config**: canonical `max_pos=2` (spec change from T1 proposal mp=1 to DA T6 empirical best mp=2), spread_threshold_pts=5, non-|pos|-gated
- **Instrument**: TMFD6 (sibling of C33 TXFD6 prior-run PROMOTE)
- **Raw PnL**: +4,557 NTD/day (fresh CK-direct, 20 days)
- **Shadow target 30%**: +1,367 NTD/day
- **Cost profile**: robust across inst/retail RT (-30% to +33%); retail 4pt stays +3,270/day
- **Statistical signal**: daily t=1.228 (non-sig at α=0.10); large per-trip sample (959 trips)
- **Drawdown profile**: MaxDD/cum 54% (fragile tail)
- **6 promotion conditions** including `requires_broker_confirmation_before_live: true`, 5-day rolling loss trigger, regime monitor
- **Shadow scaffold complete**: `src/hft_platform/strategies/c60_tmfd6_solo_maker.py` + 33/33 wrapper tests + RELEASE_GATE.md + SHADOW_DEPLOY.md + manifest + config entries
- **Status**: awaiting manual shadow→live approval

### 2. C63 TXFD6 R47 tighter-spread (R2-SUPP PROMOTE_CONDITIONAL, REOPENED from SELF-KILL T1)

- **Process note**: R2 original T1 SELF-KILL on C63 was based on wrong-instrument analogy (used r47_tmfd6_economics.md §1 TMFD6 data as proxy for TXFD6). Executor ran T5 as kill-validation and discovered fresh CK-direct contradicted T1. Lead reopened as R2-SUPP; DA T6 delivered PROMOTE_CONDITIONAL.
- **Config**: canonical `sp=3, mp=3` (tighter spread threshold than C33's sp=5), non-|pos|-gated
- **Instrument**: TXFD6 (REPLACES deployed C33)
- **Raw PnL**: +114,680 NTD/day (2.33× C33 baseline at sp=5)
- **Shadow target 30%**: +34,404 NTD/day
- **Cost profile**: FRAGILE — break-even RT 2.83pt; sign-flips at retail 3pt (-14,447/day) and catastrophic at retail 4pt (-100,532/day)
- **Statistical signal**: daily t=2.725 (passes α=0.05)
- **Drawdown profile**: MaxDD/cum 7.8% (much better than C60)
- **8 promotion conditions** including **HARD COST GATE: if broker confirms RT > 2.5pt, DO NOT deploy**, `replaces_c33: true` (mutually exclusive), regime monitor, walk-forward k=5
- **Shadow scaffold complete**: `src/hft_platform/strategies/c63_txfd6_tight_spread_maker.py` + 31/31 wrapper tests + RELEASE_GATE.md + SHADOW_DEPLOY.md + manifest + config entries
- **Status**: awaiting manual shadow→live approval

## KILL taxonomy (9 kills across 5 failure-mode categories)

### 1. Cost-wall / fee-dimension failures (3 kills)

| Round | Candidate | Kill reason |
|---|---|---|
| R3 | C64 TXFD6 rebate-widening | Rebate/adverse ratio 0.06 (dimensional dead on TXFD6, viable on TMFD6 as C73 salvage) |
| R8 | C70 TXO ATM maker | Inst RT 35pt > median ATM spread 30pt; fee>spread structural |
| R10 | C74 TXF-TMF basis MR | Break-even 3.2pt combined inst (0 margin), -3-4pt underwater at retail 6-7pt |

### 2. Trigger-saturation / overlay-axis-exhaustion (2 kills)

| Round | Candidate | Kill reason |
|---|---|---|
| R5 | C72 TMFD6 queue-position-aware | Dominated by C60 baseline; R47 D2 axis disabled; Lipton-Pesavento thin-queue is adverse for that-side maker |
| R6 | C71 fill-rate predictor | 14 prior kill overlaps; direct R23 toxicity feature [21] match (kept NOT tradeable); horizon exhausted |
| R9 | C73 TMFD6 rebate-widening-on-C60 | Dimensional prerequisite MET but all 7 plausible triggers overlap killed directions — trigger-axis saturation |

### 3. Cross-instrument physics (1 kill)

| Round | Candidate | Kill reason |
|---|---|---|
| R7 | C66 TXF-TMF pair MM | Hedge-take cost 870 NTD × 17 = dominates 50 NTD TMF maker gross; 20-TMF quote dominates L1 queue. Not rescued by cost reduction. |

### 4. Mechanism-empirical failures (2 kills)

| Round | Candidate | Kill reason |
|---|---|---|
| R4 | C68 TXF rollover maker | 150× trip-count overstatement; adverse-selection-concentrated transition window |
| R11 | C78 TXFD6 monthly-regime-adaptive | Monthly regime is contract-lifecycle artifact, not cyclical; live classifier never fires |

## Meta-findings

### 1. Physics-first-principles rule (R7 C66 correction)

Lead's R7 dispatch used wrong hedge ratio (1:5); DA caught (1:20 correct). Rule added: "All hedge/cross-instrument math must derive dollar-neutral ratios from point_value specs first-principles, not from dispatch numbers. Cross-check against kill-class history."

### 2. Instrument-matched data rule (R2-SUPP correction)

Researcher's R2 T1 used TMFD6 bracket-sweep data as proxy for TXFD6, producing wrong numbers. Rule added: "T1 self-KILL must use instrument-matched CK data; never proxy from sibling instrument."

### 3. T2 formula mini-sim rule (R10 C74 correction)

DA T2 approved C74 without catching `basis = mid_txf − 20·mid_tmf` dimensional error. Rule added: "T2 APPROVE conditional on 1-day CK mini-sim validation of any basis/signal/hedge formula BEFORE T4 dispatch. Nonsensical magnitude = T2 FAIL."

### 4. Trigger-axis exhaustion (R9 structural finding)

Maker active-overlay class has saturated 7 trigger axes (queue-depth/imbalance/vol/Hawkes/ML-fill-rate/post-fill-reactive/vol-adaptive). Future re-admit requires trigger from macro-event calendar, options-IV-surface-shape, or long-horizon (>15min) realized vol.

### 5. Cost-fragility spectrum across PROMOTEs

| Candidate | Break-even RT | Retail (3-4pt) survives? |
|---|---:|---|
| C60 | ~4+ pt | YES (+3,270 @ retail 4pt) |
| C63 | 2.83 pt | NO (-14K @ retail 3pt) |
| C74 (killed) | 3.2 pt combined | NO catastrophic (-2M @ retail 6pt combined) |

All PROMOTEs require `requires_broker_confirmation_before_live: true` flag. User's explicit authorization of ESTIMATED cost model is the load-bearing assumption.

## Artifacts

- Per-round summaries: `round-{1..11}/summary.md`
- R2-SUPP supplemental: `round-2/summary-supplemental.md`
- Progress log: `progress.jsonl` (full event log)
- Candidate pool + killed_directions: `candidate_pool.json`, `shared-context.yaml#killed_directions`
- Resume checkpoint: `resume_checkpoint.json` (status: halted_budget_guard)
- Budget: `budget.json`
- Regen artifacts: `regen-1-proposals.md`, `regen-1-sanity.md`, `regen-1-context.md`
- C60 PROMOTE package: `research/alphas/c60_tmfd6_r47_minimal_inst_rt/` + `src/hft_platform/strategies/c60_tmfd6_solo_maker.py`
- C63 PROMOTE package: `research/alphas/c63_txfd6_r47_tight_spread/` + `src/hft_platform/strategies/c63_txfd6_tight_spread_maker.py`

## Next manual actions (user-gated)

1. **Review C60 shadow-deploy package** at `research/alphas/c60_tmfd6_r47_minimal_inst_rt/SHADOW_DEPLOY.md`. Issue manual approval if accepting shadow phase.
2. **Confirm broker RT with institutional tier** BEFORE any live deployment of C60 or C63. User's ESTIMATED cost_model is the load-bearing assumption; actual broker-confirmed RT governs viability.
3. **C63 vs C33 decision**: C63 REPLACES C33 on TXFD6 (mutually exclusive). User must disable C33 before enabling C63. If broker RT > 2.5pt retail, C63 is DO NOT DEPLOY.
4. **Configure shadow monitoring** per SHADOW_DEPLOY.md auto-disable rules (both C60 and C63 have 6-7 canonical rules each).
5. **Decide fresh run trigger**: wait for TXFE6 transition data (post-2026-04-15+) to re-admit C68 (PARKED). Or extend fee-tier / options infra scope.

## Resume guidance (for future runs)

1. **Do NOT resume this run** — halt is clean structural-exhaustion, not regen-stuck.
2. **Fresh run preconditions to change**:
   - (a) Broker-confirm actual RT tier (inst vs retail) — dissolves "ESTIMATED" fragility for C60/C63
   - (b) New data regime (TMFD6 spread re-expansion, TXFD6 compression, or TXFE6 rollover-week data)
   - (c) F1/F2 options infrastructure upgrade (unlocks multi-leg TXO candidates blocked in this run)
3. **Apply new process rules** from this run as first-round filter:
   - R2-SUPP instrument-matched-data rule
   - R7 physics-first-principles rule
   - R10 T2 mini-sim formula validation rule
4. **Record C60+C63 cost-fragility profile** as priors — future TXFD6 candidates must clear retail-cost gate.

Run ended cleanly. Team idle. Pending user action on 2 PROMOTE shadow packages.
