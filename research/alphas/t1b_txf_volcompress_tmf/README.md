# T1-B — TXF Volatility-Compression → TMF Directional Expansion

**Track:** T1 (TXF higher-timeframe regime → TMF expression). See `track_t1_opened_2026_05_13`.
**Status:** V0 viability audit (Stage-1 hard gate). NOT promotion-eligible until the
hard gate passes and the formal Gate A–F pipeline produces a `mean_net_edge_pts_per_trade`
artifact. No live wiring exists.
**Created:** 2026-06-02.

This is the fixed pre-registered spec required by the research SOP (12 fields). It is
**frozen** before the first run: no parameter search, no same-day distribution thresholds,
no post-hoc rule edits (per `txf_led_research_discipline` rules 7, 12, 19).

| Field | Value |
| --- | --- |
| **strategy_name** | `t1b_txf_volcompress_tmf` |
| **market** | TAIFEX futures (day session 08:45–13:45 Asia/Taipei). |
| **instrument** | Signal = TXF (TXFB6/C6/D6/E6 front quarter). Execution = TMF (TMFB6/C6/D6/E6). TXF→TMF, single-leg directional. |
| **hypothesis** | After an intraday volatility *compression* (realized vol over a 30-min window falls to ≤70% of the prior 30-min baseline — a "coil"), the subsequent directional break out of the compression range carries higher-timeframe regime persistence that TMF can express with controlled risk. Edge source = post-compression expansion / regime persistence, **not** L2 microstructure lead–lag (that lane is closed). |
| **timeframe** | Intraday higher-timeframe: 30-min baseline + 30-min compression + 30-min break window; anchor slides every 5 min. One TXF regime axis; L2 used only for executable bid/ask + quote sanity. |
| **holding_period** | 15–60 min (evaluated at 15/30/60-min horizons; headline = 30 min). 60-min cooldown enforces no overlapping trades. |
| **entry_rule** | At anchor `t0`: require `rv(compression_window)/rv(baseline_window) ≤ 0.70` (genuine compression, backward-looking only). Compression range = [min mid, max mid] over the compression window. Enter on the **first** break in `[t0, t0+30min]` where TXF mid ≥ range_high + 8 pt (long) or ≤ range_low − 8 pt (short). Require break aligned with trade VWAP (long: entry_ref > VWAP; short: entry_ref < VWAP). TMF entry = executable **ask** (long) / **bid** (short) at/after the TXF trigger time — never mid. |
| **exit_rule** | Time-based exit at the horizon (15/30/60 min) on the executable opposite side (long exit = TMF **bid** path, short exit = TMF **ask** path). Stop structure = compression-range **opposite side** (long stop = range_low; short stop = range_high); `stop_structure_breached` recorded when the post-entry path touches it. |
| **position_sizing** | Fixed 1 lot per event (V0). No scaling, no pyramiding, no \|pos\|-gating (avoids C22-class meta-kill). |
| **risk_control** | No-overlap (60-min cooldown after each entry); executable bid/ask only (no mid fills); invalid-quote guard via BBO reconstruction (bid<ask, qty>0); single-event-per-anchor; session-bounded (no events whose break window exceeds session end). HALT/force-flat semantics inherited from platform at any future live stage (not modified here). |
| **cost_model** | TMF executable bid/ask captures the spread; **8 pt round-trip** fee+tax+slippage applied on top per `txf_led_research_discipline` operating envelope (`feedback_taifex_fee_structure`, conservative ≈4 ticks). `net_after_cost = gross_executable_return − 8`. TMF point value 10 NTD/pt. Latency (P99 ~500 ms) NOT yet applied at V0 — parity with T1-A V0; flagged as a deferred refinement before any promotion. |
| **validation_plan** | Stage-1 V0 hard gate (`track_t1_opened`): ≥20 trading days, ≥80 events, B6/C6/D6/E6 all present, executable bid/ask, 8-pt cost, median net > 0, p10 not catastrophic, remove-best-1 ≥0/near-flat, stop-breach rate < H9-family baseline, **no single-contract concentration**, **no single-day-dominance**. Data: full paired span 2026-01-26 → 2026-04-15; recency-respecting OOS split at 2026-03-26 (IS 01-26→03-25, OOS 03-26→04-15) per `feedback_backtest_recency_bias`. Goal #5 floor: report median/mean `net_after_cost` vs **>10 pt**. Verdict ∈ {PROCEED, KILL, NEEDS-MORE-DAYS}. Only on PROCEED does it advance to the Gate A–F pipeline (`hft alpha pipeline run`) that emits the governed `mean_net_edge_pts_per_trade` artifact subject to the hardened edge-evidence + parity audits. |

## Pre-registered kill/keep rule (frozen)

- **KILL** if median `net_after_cost_30m` ≤ 0, OR remove-best-1 median collapses, OR
  single-day net share > ~50%, OR a single contract supplies the entire positive PnL,
  OR stop-breach rate is at H9-family levels (≥50%).
- **NEEDS-MORE-DAYS** if the distribution is non-negative but events < 80, days < 20, or
  a contract is missing — promising but undersized (mark `needs_more_sample`, never "complete").
- **PROCEED** only if the full hard gate passes AND median net > 0; separately flag whether
  median/mean net clears the **>10** goal floor.

No re-optimization counts are spent until the first scorecard exists. Max 3 re-optimizations
per `txf_led_research_discipline`; same-sample re-tuning is forbidden.

## How to run the V0 audit

```bash
uv run python -m research.t1.regime_viability \
  --mode vol_compression \
  --raw-dir research/data/raw \
  --months B6,C6,D6,E6 \
  --max-date 2026-04-15 \
  --oos-start 2026-03-26 \
  --out-dir research/experiments/validations/t1b_volcompress_v0
```
