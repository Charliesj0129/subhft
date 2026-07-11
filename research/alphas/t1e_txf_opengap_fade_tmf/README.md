# T1-E — TXF Open-Gap Overreaction Fade → TMF

**Track:** T1 (TXF higher-timeframe regime → TMF expression). See `track_t1_opened_2026_05_13`.
**Status:** V0 viability audit (Stage-1 hard gate). NOT promotion-eligible until the
hard gate passes and the formal Gate A–F pipeline produces a `mean_net_edge_pts_per_trade`
artifact. No live wiring exists.
**Created:** 2026-06-03.
**Origin:** User-initiated hypothesis (H2 from the 2026-06-03 paper×data menu), chosen after
the **T1-D intraday-momentum KILL** showed the open→close relation on TXF/TMF is *reversal*-signed
(62% of momentum trades wrong-signed). H2 is the directionally opposite (fade) thesis. Primary
anchor: **"Do the prices of stock index futures in Asia overreact to U.S. market returns?"
*Pacific-Basin Finance Journal* (2009)** — Taiwan/Korea/HK/Singapore/Japan index futures overreact
to overnight information, implying a partial intraday reversal; index-futures-level overnight→first
window relation is negative (reversal) in the ETF/futures overnight literature (IRFE 2017).

This is the fixed pre-registered spec (12 fields). It is **frozen** before the first run: no
parameter search, no same-day distribution thresholds, no post-hoc rule edits (per
`txf_led_research_discipline` rules 7, 12, 19).

| Field | Value |
| --- | --- |
| **strategy_name** | `t1e_txf_opengap_fade_tmf` |
| **market** | TAIFEX futures (day session 08:45–13:45 Asia/Taipei). |
| **instrument** | Signal = TXF (TXFB6/C6/D6/E6 front quarter). Execution = TMF. TXF→TMF, single-leg directional. |
| **hypothesis** | A large prior-session-close→today-open gap (overnight overreaction) partially reverses during the day session. Fade it: gap up → short, gap down → long. Edge = open-gap overreaction / partial reversal, **not** L2 microstructure lead–lag. |
| **timeframe** | Higher-timeframe session structure: prior-session close vs today's open (overnight gap), faded intraday. L2 only for executable bid/ask + quote sanity. |
| **holding_period** | One trade per contract-day. Enter ~09:00 (open + 15-min confirm); hold to the 30-min headline horizon (also 15/60-min). |
| **entry_rule** | `gap = today_open_mid − mean(prior_session_final_30min_mid)` (endogenous, no external EOD). Require `\|gap\| ≥ 15 TXF pts`. Direction = `−sign(gap)` (fade). Enter at open+15min. TMF entry = executable **ask** (long) / **bid** (short). |
| **exit_rule** | Time-based exit at 15/30/60-min horizons on the executable opposite side (long exit = TMF **bid** path, short = TMF **ask** path). Stop structure = gap **extends** past today's open by a 15-pt buffer (continuation against the fade); `stop_structure_breached` recorded when the post-entry path touches it. |
| **position_sizing** | Fixed 1 lot per event (V0). No scaling/pyramiding/\|pos\|-gating. |
| **risk_control** | One entry per contract-day; executable bid/ask only (no mid fills); gap-extension stop (15 pt); invalid-quote guard via BBO reconstruction (bid<ask, qty>0); session-bounded. HALT/force-flat inherited from platform at any future live stage. |
| **cost_model** | TMF executable bid/ask captures the spread; **8 pt round-trip** fee+tax+slippage on top per `txf_led_research_discipline` (`feedback_taifex_fee_structure`). `net_after_cost = gross_executable_return − 8`. TMF point value 10 NTD/pt. Latency (P99 ~500 ms) NOT applied at V0 — parity with T1-A/B/D V0; deferred refinement. |
| **validation_plan** | Stage-1 V0 hard gate: ≥20 trading days, ≥80 events, B6/C6/D6/E6 all present, executable bid/ask, 8-pt cost, median net > 0, p10 not catastrophic, remove-best-1 ≥0/near-flat, stop-breach < H9 baseline (0.50), no single-contract concentration, no single-day-dominance, drawdown ≤ 2× avg monthly net. Data: full paired span 2026-01-26 → 2026-04-15; OOS split 2026-03-26 per `feedback_backtest_recency_bias`. Goal #5 floor: median/mean `net_after_cost` vs **>10 pt**. Verdict ∈ {PROCEED, KILL, NEEDS-MORE-DAYS}. PROCEED → Gate A–F pipeline. |

## Caveat on the endogenous gap

The gap uses **day-session boundaries** (prior 13:45 close vs today 08:45 open), not the official
settlement price, and folds in the TAIFEX night session that trades between them. This is recorded
as a promotion blocker (`endogenous_gap_uses_day_session_boundaries_not_settlement_price`): a
governed version would reconcile against settlement prices before promotion.

## Pre-registered kill/keep rule (frozen)

- **KILL** if median `net_after_cost_30m` ≤ 0, OR remove-best-1 median collapses, OR single-day
  net share > ~50%, OR a single contract supplies all positive PnL, OR stop-breach ≥ 0.50, OR
  drawdown > 2× avg monthly net.
- **NEEDS-MORE-DAYS** if non-negative but events < 80, days < 20, or a contract is missing.
- **PROCEED** only if the full hard gate passes AND median net > 0; flag whether net clears **>10**.

Max 3 re-optimizations per `txf_led_research_discipline`; same-sample re-tuning forbidden.

## How to run the V0 audit

```bash
uv run python -m research.t1.regime_viability \
  --mode open_gap_fade \
  --raw-dir research/data/raw \
  --months B6,C6,D6,E6 \
  --max-date 2026-04-15 \
  --oos-start 2026-03-26 \
  --out-dir research/experiments/validations/t1e_opengap_fade_v0
```
