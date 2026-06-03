# T1-D — TXF Intraday Session Momentum → TMF Directional Expression

**Track:** T1 (TXF higher-timeframe regime → TMF expression). See `track_t1_opened_2026_05_13`.
**Status:** V0 viability audit (Stage-1 hard gate). NOT promotion-eligible until the
hard gate passes and the formal Gate A–F pipeline produces a `mean_net_edge_pts_per_trade`
artifact. No live wiring exists.
**Created:** 2026-06-03.
**Origin:** User-initiated hypothesis selected from a paper×data cross-referenced menu.
Primary anchor: **Gao, Han, Li, Zhou, "Market Intraday Momentum," *Journal of Financial
Economics* 129 (2018)** — the first-half-hour market return predicts the last-half-hour
return with the same sign (R² ≈ 1.6–2.6%; replicates on 10 international index ETFs;
strongest on high-volatility / high-volume / macro-news days). This is a SESSION-STRUCTURE
momentum effect, distinct from (and orthogonal to) the already-killed sub-5-minute
order-flow / L2 microstructure momentum family.

This is the fixed pre-registered spec required by the research SOP (12 fields). It is
**frozen** before the first run: no parameter search, no same-day distribution thresholds,
no post-hoc rule edits (per `txf_led_research_discipline` rules 7, 12, 19).

| Field | Value |
| --- | --- |
| **strategy_name** | `t1d_txf_intraday_momentum_tmf` |
| **market** | TAIFEX futures (day session 08:45–13:45 Asia/Taipei). |
| **instrument** | Signal = TXF (TXFB6/C6/D6/E6 front quarter). Execution = TMF (TMFB6/C6/D6/E6). TXF→TMF, single-leg directional. |
| **hypothesis** | The TXF return over the first 30-min window of the session (08:45–09:15) carries directional information that persists into the last 30-min window (13:15–13:45), with the SAME sign — informed/positioning flow established early in the session re-asserts into the close. Edge source = session-structure intraday momentum (Gao et al. 2018), **not** L2 microstructure lead–lag (closed lane). |
| **timeframe** | Intraday session structure: 30-min open (signal) window + 30-min predict (trade) window inside the 300-min day session. One signal axis (TXF open-window return); L2 used only for executable bid/ask + quote sanity. |
| **holding_period** | One trade per contract-day. Enter at the start of the last window (13:15); hold to session close (13:45) — a ~30-min hold (also evaluated at 15/60-min horizons; headline = 30 min). No overlapping trades (one entry per day by construction). |
| **entry_rule** | Compute `ret_open = mid(open_window_close) − mid(session_open)` over 08:45–09:15. Require `\|ret_open\| ≥ 10 TXF pts` (absolute backward-looking magnitude filter — captures a genuine directional morning and screens flat sessions; NOT a cross-day percentile, so no look-ahead). Direction = `sign(ret_open)`. At 13:15 require entry aligned with session trade VWAP (long: TXF entry_ref > VWAP; short: < VWAP). TMF entry = executable **ask** (long) / **bid** (short) at/after 13:15 — never mid. |
| **exit_rule** | Time-based exit at the horizon (15/30/60 min from entry; headline = session close 13:45) on the executable opposite side (long exit = TMF **bid** path, short exit = TMF **ask** path). Stop structure = the **opposite side of the open-window range** (long stop = open_window_low, short stop = open_window_high — a full give-back of the morning move); `stop_structure_breached` recorded when the post-entry path touches it. |
| **position_sizing** | Fixed 1 lot per event (V0). No scaling, no pyramiding, no \|pos\|-gating (avoids C22-class meta-kill). |
| **risk_control** | One entry per contract-day; executable bid/ask only (no mid fills); invalid-quote guard via BBO reconstruction (bid<ask, qty>0); session-bounded (predict window inside the day session). HALT/force-flat semantics inherited from platform at any future live stage (not modified here). |
| **cost_model** | TMF executable bid/ask captures the spread; **8 pt round-trip** fee+tax+slippage applied on top per `txf_led_research_discipline` operating envelope (`feedback_taifex_fee_structure`, conservative ≈4 ticks). `net_after_cost = gross_executable_return − 8`. TMF point value 10 NTD/pt. Latency (P99 ~500 ms) NOT yet applied at V0 — parity with T1-A/B V0; flagged as a deferred refinement before any promotion. |
| **validation_plan** | Stage-1 V0 hard gate (`track_t1_opened`): ≥20 trading days, ≥80 events, B6/C6/D6/E6 all present, executable bid/ask, 8-pt cost, median net > 0, p10 not catastrophic, remove-best-1 ≥0/near-flat, stop-breach rate < H9-family baseline (0.50), **no single-contract concentration**, **no single-day-dominance**, drawdown ≤ 2× average monthly net. Data: full paired span 2026-01-26 → 2026-04-15; recency-respecting OOS split at 2026-03-26 (IS 01-26→03-25, OOS 03-26→04-15) per `feedback_backtest_recency_bias`. Goal #5 floor: report median/mean `net_after_cost` vs **>10 pt**. Verdict ∈ {PROCEED, KILL, NEEDS-MORE-DAYS}. Only on PROCEED does it advance to the Gate A–F pipeline (`hft alpha pipeline run`) that emits the governed `mean_net_edge_pts_per_trade` artifact subject to the hardened edge-evidence + parity audits. |

## Pre-registered kill/keep rule (frozen)

- **KILL** if median `net_after_cost_30m` ≤ 0, OR remove-best-1 median collapses, OR
  single-day net share > ~50%, OR a single contract supplies the entire positive PnL,
  OR stop-breach rate is at H9-family levels (≥50%), OR drawdown > 2× avg monthly net.
- **NEEDS-MORE-DAYS** if the distribution is non-negative but events < 80, days < 20, or
  a contract is missing — promising but undersized (mark `needs_more_sample`, never "complete").
- **PROCEED** only if the full hard gate passes AND median net > 0; separately flag whether
  median/mean net clears the **>10** goal floor.

No re-optimization counts are spent until the first scorecard exists. Max 3 re-optimizations
per `txf_led_research_discipline`; same-sample re-tuning is forbidden.

## How to run the V0 audit

```bash
uv run python -m research.t1.regime_viability \
  --mode intraday_momentum \
  --raw-dir research/data/raw \
  --months B6,C6,D6,E6 \
  --max-date 2026-04-15 \
  --oos-start 2026-03-26 \
  --out-dir research/experiments/validations/t1d_intraday_momentum_v0
```
