# C33 TXFD6 Solo Passive Maker — Release Gate

**Alpha ID**: `c33_txfd6_solo_passive_maker`
**Promotion date**: 2026-04-18 (R7 T6)
**Shadow scaffold**: R7 T8, 2026-04-18
**Status banner**: **SHADOW_SCAFFOLDED_2026-04-18; enabled=false pending user confirmation**
**Operating point**: `max_pos=3, queue_share=0.05, variant=R47-minimal, instrument=TXFD6`
**Projected daily PnL (OOS, mp=3, q=5%)**: +76,920 NTD/day; 30%-haircut floor **+23,076 NTD/day**

## Critical guardrail (user policy)

Per `memory/feedback_no_auto_deploy.md`: **"Remote deployment is always manual."**

- `strategies.yaml` entry ships with `enabled: false`.
- The shadow→live transition requires the user to **manually** set
  `enabled: true` AND manually approve via explicit directive.
- No agent (including the Executor) may flip `enabled` without user approval.

## Shadow→Live Checklist

All items must be confirmed before flipping `enabled: true`.

- [ ] **Shadow session count**: ≥ 5 consecutive trading sessions with C33
      running under `HFT_ORDER_SHADOW_MODE=1`.
- [ ] **Regime qualification**: all 5 shadow sessions had TXFD6 session
      median spread ≥ 4 pt. (Shadow-kill: `sp_med < 3` for 3 consec days).
- [ ] **close_maker_rate**: ≥ 80% over the first 200 shadow cycles.
      (DA T2 decisive condition; T5 realized 97.7% OOS at mp=3 q=5%.)
- [ ] **Daily PnL floor**: mean shadow daily PnL ≥ +7,000 NTD/day over the
      5 sessions. (30% haircut vs +23,076 NTD/day projected floor.)
- [ ] **Auto-disable triggers**: no auto-disable fired in any shadow session
      (regime-persistence, close_maker_rate floor, daily loss hard stop).
- [ ] **Loss-tail**: worst single shadow day loss / mean shadow daily PnL
      remains < 5× ratio.
- [ ] **OOS days-positive (shadow)**: ≥ 60% of sessions positive (3/5).
- [ ] **Config parity**: shadow-run strategies.yaml params MATCH the
      T5 winning combo (max_pos=3, spread_threshold_pts=5,
      inventory_skew_tenths=2, R47-minimal variant).
- [ ] **Risk limits parity**: `strategy_limits.yaml` C33_TXFD6_SOLO_MAKER
      entry matches shadow run; hard stops active.
- [ ] **No concurrent C14 on TXFD6**: `C14_TXF_FRONTMONTH_MAKER` stays
      `enabled: false` (both on TXFD6 would double-book position accounting).
- [ ] **No concurrent C27 on TXFD6**: `C27_VOL_AMPLIFIED_C14` stays
      `enabled: false` (C27 replaces C14; same instrument conflict).
- [ ] **User manual approval**: user explicitly confirms "promote C33 to live".

## Post-shadow engineering follow-ups (NOT blockers for live)

These are items to schedule after a clean shadow but before full-scale
live deployment:

1. **Front-month rotation**: C33 currently pinned to TXFD6. If/when TXFD6
   rotates to back-month, a TXF rolling front-month rotator (analogous
   to C14's `TxfFrontMonthMaker`) would need to extend the strategy to
   `{TXFB6, TXFC6, TXFD6}`. Not required for initial shadow; production
   live can start on TXFD6 alone.
2. **R47 signal-layer calibration on TXFD6**: R7 T1 counterfactual
   rejected TMFD6-calibrated layers (R47-full-QI underperforms R47-minimal
   4:1 on TXFD6). A TXFD6-specific calibration of PE / Queue / MFG / QI
   may offer incremental improvement after shadow validates the baseline.

## Artifacts

- Strategy code: `research/alphas/c33_txfd6_solo_passive_maker/impl.py`
- Manifest: `research/alphas/c33_txfd6_solo_passive_maker/manifest.yaml`
- Live wrapper: `src/hft_platform/strategies/c33_txfd6_solo_maker.py`
- strategies.yaml entry: `C33_TXFD6_SOLO_MAKER` (enabled: false)
- strategy_limits.yaml entry: `C33_TXFD6_SOLO_MAKER`
- T5 scorecard: `outputs/team_artifacts/alpha-research/round-7/artifacts/executor_t5_scorecard.md`
- T5 bracket sweep: `outputs/team_artifacts/alpha-research/round-7/artifacts/executor_t5_bracket_sweep.md`
- DA T2 review: `outputs/team_artifacts/alpha-research/round-7/artifacts/da_t2_kill_checklist.md`

## Cost citation (binding)

TXF RT = **3.0 pt** retail, per `memory/feedback_taifex_fee_structure.md`
(user-confirmed 2026-04-18). Per-cycle cost: 3 pt × 200 NTD/pt = **600
NTD per full cycle**. Single-instrument — no hedge leg, no cross-instrument
qty scaling (R5 hedge-qty rule N/A).
