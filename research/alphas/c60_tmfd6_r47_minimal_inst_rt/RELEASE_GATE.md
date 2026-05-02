# C60 TMFD6 R47-minimal Maker (inst RT) — Release Gate

```
⚠️ SHADOW DEPLOY — MANUAL USER APPROVAL REQUIRED BEFORE LIVE
- Cost model: ESTIMATED (inst tier, not broker-confirmed)
- If broker confirms RT > 2.5 pt, REOPEN promotion decision
- Shadow target: +1,367 NTD/day at 30% scale (from +4,557 raw at mp=2)
- 6 auto-disable rules in SHADOW_DEPLOY.md
```

**Alpha ID**: `c60_tmfd6_r47_minimal_inst_rt`
**Promotion date**: 2026-04-19 (R1 T6 PROMOTE_CONDITIONAL)
**Shadow scaffold**: R1 T8, 2026-04-19
**Status banner**: **SHADOW_SCAFFOLDED_2026-04-19; enabled=false pending user confirmation**
**Operating point (canonical)**: `max_pos=2, spread_threshold_pts=5, inventory_skew_tenths=2, qi_skew_threshold=0.10, qi_skew_widen_ticks=1, enable_qi_layer=true, variant=R47-minimal, instrument=TMFD6`
**Projected daily PnL (fresh CK-direct OOS, mp=2, qf=1.0, 20 days)**: +4,557 NTD/day; 30%-haircut floor **+1,367 NTD/day**

## Critical guardrails (BINDING)

### Guardrail 1 — Broker confirmation required (DA T2 flag #1)

Per `shared-context.yaml#cost_model.notes` and DA T2 Tier-1 Physics Audit
finding H1(a):

**`requires_broker_confirmation_before_live: true`** — MUST be resolved
before `enabled: true`. The `TMF RT = 1.5 pt` and `maker_rebate = 10 NTD/side`
are institutional-tier ESTIMATES (`cost_model.confirmed: false`,
user-authorized rough estimate 2026-04-19). Live deployment requires:

- [ ] Broker-contract confirmation of TMF RT (inst tier) in pts.
- [ ] Broker confirmation of maker rebate (or confirmation that there is NO rebate).
- [ ] Broker confirmation of tax MM discount (50% assumed; must verify).
- [ ] If any of the above differs materially, re-run T5 fresh CK-direct at the
      confirmed RT and re-assess PROMOTE verdict.

### Guardrail 2 — User manual deploy (user policy)

Per `memory/feedback_no_auto_deploy.md`: **"Remote deployment is always manual."**

- `strategies.yaml` entry ships with `enabled: false`.
- The shadow-first lifecycle requires the user to **manually** set
  `enabled: true` AND manually approve via explicit directive AFTER shadow
  clears this gate.
- No agent (including the Executor) may flip `enabled` without user approval.

## Shadow-Only Checklist (Phase 1)

All items must be confirmed before considering shadow -> live:

- [ ] **Shadow session count**: >= 5 consecutive trading sessions with C60
      running under `HFT_ORDER_SHADOW_MODE=1`.
- [ ] **Regime qualification**: all 5 shadow sessions had TMFD6 session
      median spread >= 2 pt. (Shadow-kill: `sp_med < 1` for 3 consec days.)
- [ ] **close_maker_rate**: >= 80% over the first 200 shadow cycles.
      (DA T2 decisive condition; T5 realized 100.0%.)
- [ ] **Daily PnL floor**: mean shadow daily PnL >= +1,367 NTD/day over
      5 sessions (30% haircut vs +4,557 NTD/day projected).
- [ ] **Rolling PnL monitor (DA T6 cond #2)**: 5-day rolling mean daily PnL
      does NOT drop below -3,000 NTD/day for 2+ windows.
- [ ] **Walk-forward consistency (DA T6 cond #4)**: in any rolling 5-session
      block, at least 3/5 sessions must be positive net.
- [ ] **Fresh-sim vs projection monitor**: 5-day rolling actual PnL stays >=
      10% of +4,557 projection (i.e., >= +456 NTD/day floor).
- [ ] **Spread regime shift (DA T6 cond #3)**: no structural 5-day shift of
      TMFD6 median spread <= 1 pt or >= 5 pt. Either direction invalidates
      the current operating assumption.
- [ ] **Loss-tail**: worst single shadow day loss / mean shadow daily PnL
      remains < 5x ratio.
- [ ] **OOS days-positive (shadow)**: >= 60% of sessions positive (3/5).
- [ ] **Config parity**: shadow-run strategies.yaml params match the T5
      canonical (max_pos=2, spread_threshold_pts=5, inventory_skew_tenths=2,
      qi_skew_threshold=0.10, qi_skew_widen_ticks=1, enable_qi_layer=true,
      R47-minimal variant).
- [ ] **Risk limits parity**: `strategy_limits.yaml` C60_TMFD6_SOLO_MAKER
      entry matches the shadow run; auto_disable triggers active.
- [ ] **No concurrent TMFD6 maker**: no other strategy may run on TMFD6
      concurrently (C17 TMF FrontMonth Maker, if enabled, would double-book).
- [ ] **Broker-contract confirmation** — see Guardrail 1.
- [ ] **User manual approval**: user explicitly confirms "promote C60 to live"
      AFTER all items above are satisfied.

## Live-transition Checklist (Phase 2 — after shadow clears)

**Only attempt after Phase 1 is fully checked off.** This phase requires an
additional user-initiated directive.

- [ ] User edits `strategies.yaml` `enabled: true` for the C60 entry.
- [ ] User restarts the engine (`docker compose restart hft-engine`).
- [ ] User confirms C60 is quoting via Prometheus:
      `hft_strategy_quotes_posted_total{strategy_id="C60_TMFD6_SOLO_MAKER"}`.
- [ ] First live day: max_position_lots hard-capped at 2 (strategy_limits.yaml).
- [ ] Rollback readiness: user knows how to set `enabled: false` and restart.

## Post-shadow engineering follow-ups (NOT blockers for live)

1. **Front-month rotation**: C60 pinned to TMFD6. If/when TMFD6 rotates to
   back-month, a TMF rolling front-month rotator (analogous to C17) would
   need to extend the strategy to `{TMFB6, TMFC6, TMFD6}`. Not required for
   initial shadow; shadow-only starts on TMFD6 alone.
2. **Rebate confirmation & rebate-aware quoting**: if broker confirms rebate
   >= 2 pt/RT, the existing D4 QI layer may be augmented with a rebate-aware
   quote-widening layer. OUT OF SCOPE for this shadow; would require a
   fresh candidate (C64 in pool).
3. **mp=3 under narrower regime**: if TMFD6 spread regime shifts wider,
   mp=3 may outperform mp=2 per R47 V-shape structural properties. Re-run
   T5 scorecard if 30-day rolling median spread rises >= +1 pt from baseline.
4. **Live-regime cost re-calibration**: after live, measure actual
   fee/rebate from TCA module and replace inst-estimate with broker-confirmed
   values in `shared-context.yaml#cost_model` for all future candidates.

## Artifacts

- Strategy code: `research/alphas/c60_tmfd6_r47_minimal_inst_rt/impl.py`
- Manifest: `research/alphas/c60_tmfd6_r47_minimal_inst_rt/manifest.yaml`
- Live wrapper: `src/hft_platform/strategies/c60_tmfd6_solo_maker.py`
- strategies.yaml entry: `C60_TMFD6_SOLO_MAKER` (enabled: false)
- strategy_limits.yaml entry: `C60_TMFD6_SOLO_MAKER`
- T5 scorecard: `outputs/team_artifacts/alpha-research/round-1/artifacts/executor_t5_scorecard.md`
- T5 raw JSON: `outputs/team_artifacts/alpha-research/round-1/artifacts/executor_t5_results.json`
- DA T2 Kill Checklist: `outputs/team_artifacts/alpha-research/round-1/artifacts/da_t2_kill_checklist.md`

## Cost citation (BINDING, but ESTIMATED)

TMF RT = **1.5 pt** institutional-tier ESTIMATE, per
`outputs/team_artifacts/alpha-research/shared-context.yaml#cost_model.TMF`.
Per-cycle cost: 1.5 pt x 10 NTD/pt = **15 NTD per full cycle** at RT inst.
Retail reference: 4 pt RT = 40 NTD per full cycle (see
`memory/feedback_taifex_fee_structure.md`). Single-instrument — no hedge
leg, no cross-instrument qty scaling (R5 hedge-qty rule N/A).

**STRUCTURAL**: cost_drag = RT/median_spread = 1.5/2 = **75%** > 50%
bright-line WARN. Regime-sensitive.
