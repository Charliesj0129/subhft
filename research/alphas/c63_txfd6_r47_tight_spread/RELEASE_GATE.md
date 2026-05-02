# C63 TXFD6 R47-minimal Tight Spread — Release Gate

```
⚠️ SHADOW DEPLOY — MANUAL USER APPROVAL REQUIRED BEFORE LIVE
- Cost model: ESTIMATED (inst tier); cost-fragile: break-even RT = 2.83pt
- HARD COST GATE: If broker confirms RT > 2.5pt, DO NOT deploy
- C63 REPLACES C33 on TXFD6 — never co-deploy
- Shadow target: +34,404 NTD/day at 30% scale (from +114,680 raw at sp=3/mp=3)
- 7 auto-disable rules in SHADOW_DEPLOY.md
```

**Alpha ID**: `c63_txfd6_r47_tight_spread`
**Promotion date**: 2026-04-19 (R2-SUPP T6 PROMOTE_CONDITIONAL)
**T1 initial verdict**: SELF_KILL_T1 (reversed at R2-SUPP; wrong-instrument analogy)
**Shadow scaffold**: R2-SUPP T8, 2026-04-19
**Status banner**: **SHADOW_SCAFFOLDED_2026-04-19; enabled=false pending user confirmation**
**Operating point (canonical)**: `spread_threshold_pts=3, max_pos=3, inventory_skew_tenths=2, variant=R47-minimal-tight-spread, instrument=TXFD6`
**Projected daily PnL (fresh CK-direct OOS, sp=3/mp=3, qf=1.0, 20 days)**: **+114,680 NTD/day**; 30%-haircut floor **+34,404 NTD/day**

## Critical guardrails (7 deployment gates)

### Gate 1 — HARD COST GATE (BINDING, pre-deploy)

**If broker-confirmed TXF RT > 2.5 pt, C63 MUST NOT be deployed.**

Cost-fragility analysis (Executor R2 T5):
- Inst RT 1.5 pt: +114,680 NTD/day (sp=3/mp=3)
- Retail RT 3 pt: **-14,447 NTD/day** (sign flip)
- Retail RT 4 pt: -100,532 NTD/day (catastrophic)
- Break-even RT: ~2.83 pt
- Hard gate at 2.5 pt preserves safety margin (13% cushion)

**Action**: abort shadow-to-live transition if broker confirmation exceeds
2.5 pt. Consider PARK or revert to C33 (which survives retail).

### Gate 2 — Broker confirmation (any deploy)

`requires_broker_confirmation_before_live: true`. Same as C60 — inst RT and
rebate assumptions are ESTIMATES (`cost_model.confirmed: false`).

- [ ] Broker-contract confirmation of TXF RT (inst tier) in pts.
- [ ] Broker confirmation of maker rebate (0.1 pt/RT on TXF is negligible
      but should be confirmed).

### Gate 3 — Mutual exclusion with C33 (HARD)

**C33_TXFD6_SOLO_MAKER and C63_TXFD6_TIGHT_SPREAD_MAKER MUST NEVER both
be enabled=true.** Both target TXFD6 with the same mechanism; co-deployment
double-books positions and violates inventory accounting.

Current C33 state: `enabled: true` for 1-lot exception live rollout.
To deploy C63:
- [ ] Set C33 `enabled: false` in strategies.yaml first.
- [ ] Flat any residual C33 position on TXFD6.
- [ ] Then set C63 `enabled: true`.
- [ ] Never reverse: do not re-enable C33 without first disabling C63.

### Gate 4 — User manual deploy (user policy)

Per `memory/feedback_no_auto_deploy.md`: **"Remote deployment is always manual."**

- `strategies.yaml` ships with C63 `enabled: false`.
- No agent may flip to `enabled: true` without explicit user directive.

## Shadow-Only Checklist (Phase 1)

All items must be confirmed before shadow → live transition:

- [ ] **Shadow session count**: ≥ 5 consecutive trading sessions with C63
      running under `HFT_ORDER_SHADOW_MODE=1` (AFTER disabling C33).
- [ ] **Regime qualification**: all 5 sessions had TXFD6 session median
      spread between 3 and 6 pt (baseline 4 pt; ±20% drift triggers review).
      Auto-disable if compression <3 pt or expansion >6 pt for 3 consec days.
- [ ] **close_maker_rate**: ≥ 80% over the first 200 shadow cycles.
      (Executor T5 realized 100.0%.)
- [ ] **Daily PnL floor**: mean shadow daily PnL ≥ +34,404 NTD/day over 5
      sessions (30% haircut vs +114,680 NTD/day projected).
- [ ] **Rolling PnL floor**: 5-day rolling mean daily PnL ≥ +20,000 NTD/day
      (must not drop below for 2 consec windows).
- [ ] **Walk-forward consistency**: in any rolling 5-session block, ≥ 3/5
      positive.
- [ ] **Shortfall monitor**: 5-day rolling actual PnL stays ≥ 20% of
      +34,404 projection (i.e., ≥ +6,881 NTD/day floor).
- [ ] **Loss-tail**: worst single shadow day loss / mean shadow daily PnL
      ratio < 2× for 2 consec days.
- [ ] **Broker-contract confirmation** (Gate 1 + 2).
- [ ] **C33 disabled** (Gate 3).
- [ ] **User manual approval** (Gate 4).

## Live-transition Checklist (Phase 2 — after shadow clears)

- [ ] User sets `C33_TXFD6_SOLO_MAKER` `enabled: false` and confirms flat.
- [ ] User edits `C63_TXFD6_TIGHT_SPREAD_MAKER` `enabled: true`.
- [ ] User restarts engine.
- [ ] User confirms quoting via Prometheus:
      `hft_strategy_quotes_posted_total{strategy_id="C63_TXFD6_TIGHT_SPREAD_MAKER"}`
- [ ] max_position_lots hard-capped at 3 (strategy_limits.yaml).
- [ ] Rollback: user knows how to flip back to C33 if C63 underperforms.

## Post-shadow engineering follow-ups (NOT blockers for live)

1. Front-month rotation: C63 pinned to TXFD6. When TXFD6 rolls to
   back-month, extend to `{TXFE6, TXFF6, ...}` via rotator (same problem as
   C33 has).
2. Rebate accounting: the 0.1 pt/RT TXF rebate was confirmed dimensionally
   dead as a STRATEGY (C64 SELF_KILL), but is valid as a passive accounting
   uplift (~+2,000 NTD/day at C63 fill rate). Not load-bearing but worth
   capturing in TCA.
3. Live-regime cost re-calibration (same as C60): after live, replace
   inst-estimate with broker-confirmed values in shared-context.yaml.

## Artifacts

- Strategy code: `research/alphas/c63_txfd6_r47_tight_spread/impl.py`
- Manifest: `research/alphas/c63_txfd6_r47_tight_spread/manifest.yaml`
- Live wrapper: `src/hft_platform/strategies/c63_txfd6_tight_spread_maker.py`
- strategies.yaml entry: `C63_TXFD6_TIGHT_SPREAD_MAKER` (enabled: false)
- strategy_limits.yaml entry: `C63_TXFD6_TIGHT_SPREAD_MAKER`
- R2 T5 scorecard: `outputs/team_artifacts/alpha-research/round-2/artifacts/executor_t5_scorecard.md`
- R2 T5 raw JSON: `outputs/team_artifacts/alpha-research/round-2/artifacts/executor_t5_results.json`
- R2-SUPP reopen rationale: candidate_pool.json — T1 used TMFD6 bracket sweep as proxy
  (wrong-instrument); Executor T5 fresh CK-direct revealed +114,680 NTD/day at inst RT.

## Cost citation (BINDING, but ESTIMATED)

TXF RT = **1.5 pt** institutional-tier ESTIMATE, per
`outputs/team_artifacts/alpha-research/shared-context.yaml#cost_model.TXF`.
Per-cycle cost: 1.5 pt × 200 NTD/pt = **300 NTD per full cycle** at RT inst.
Retail reference: 3 pt RT = 600 NTD per full cycle.

**STRUCTURAL**: cost-fragile. Break-even RT = 2.83 pt (= 4.605 pt gross/trip
at T5 - net 0 cost). Any RT >2.5 pt post-confirmation requires re-evaluation.
