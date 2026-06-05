# T1-C — TXF VWAP-Trend Session Imbalance → TMF

**Track:** T1 (TXF higher-timeframe regime → TMF expression). See `track_t1_opened_2026_05_13`.
**Status:** V0 viability audit (Stage-1 hard gate). **KILL**. NOT promotion-eligible. No live wiring exists.
**Created:** 2026-06-05.
**Origin:** The third (and last un-started) frozen Track-T1 candidate, pre-registered on
2026-05-13 as *"VWAP trend / session imbalance (failed VWAP reclaim)"*, stop = VWAP reclaim /
local swing, 15–60 min hold. Built after the T1-A (INCONCLUSIVE), T1-B / T1-D / T1-E (KILL) and
T1-F (NEEDS-MORE-DAYS) candidates.

This is the fixed pre-registered spec. It is **frozen** before the first run: no parameter search,
no same-day distribution thresholds, no post-hoc rule edits (per `txf_led_research_discipline`
rules 7, 12, 19).

| Field | Value |
| --- | --- |
| **strategy_name** | `t1c_txf_vwaptrend_tmf` |
| **market** | TAIFEX futures, 08:45–13:45 Asia/Taipei day session. |
| **instrument** | Signal = TXF (TXFB6/C6/D6/E6 front quarter). Execution = TMF. TXF→TMF, single-leg directional. |
| **hypothesis** | A directional session imbalance persists: when the TXF mid has stayed predominantly on one side of the cumulative session trade VWAP and a pullback **fails to reclaim** VWAP (does not cross it), the trend **continues**. Trade WITH the trend (mid above VWAP → long, below → short). Edge = VWAP-trend persistence / failed-reclaim continuation, **not** L2 lead–lag and **not** mean reversion. |
| **timeframe** | Higher-timeframe session structure: mid vs cumulative session VWAP over a trailing 60-min window. L2 only for executable bid/ask + quote sanity. |
| **holding_period** | Slide a 5-min anchor; one entry per anchor with a 60-min no-overlap cooldown. Hold to the 30-min headline horizon (also 15/60-min). |
| **entry_rule** | At each anchor: (1) `\|mid − VWAP\| ≥ 15` TXF pts (displaced); (2) ≥ 80 % of the trailing 60-min window mids on the trend side of VWAP (persistent imbalance); (3) a *failed reclaim* = the window touched within 5 pts of VWAP but never crossed past it onto the counter-trend side. Direction = `sign(mid − VWAP)` (continuation). TMF entry = executable **ask** (long) / **bid** (short). |
| **exit_rule** | Time-based exit at 15/30/60-min horizons on the executable opposite side. Stop structure = **VWAP reclaim**: the post-entry path crosses back through the anchor VWAP by a 15-pt buffer; `stop_structure_breached` records when it does. |
| **position_sizing** | Fixed 1 lot per event (V0). No scaling/pyramiding; no re-entry within the cooldown. |
| **risk_control** | 60-min no-overlap cooldown; executable bid/ask only (no mid fills); VWAP-reclaim stop (15 pt); invalid-quote guard via BBO reconstruction (bid<ask, qty>0); session-bounded. HALT/force-flat inherited from platform at any future live stage. |
| **cost_model** | TMF executable bid/ask captures the spread; **8 pt round-trip** fee+tax+slippage on top per `txf_led_research_discipline` (`feedback_taifex_fee_structure`). `net_after_cost = gross_executable_return − 8`. TMF point value 10 NTD/pt. Latency (P99 ~500 ms) NOT applied at V0 — parity with T1-A/B/D/E/F V0; deferred. |
| **validation_plan** | Stage-1 V0 hard gate: ≥20 trading days, ≥80 events, B6/C6/D6/E6 all present, executable bid/ask, 8-pt cost, median net > 0, p10 not catastrophic, remove-best-1 ≥0, stop-breach < 0.50, no single-contract concentration, no single-day-dominance, drawdown ≤ 2× avg monthly net. Verdict ∈ {PROCEED, KILL, NEEDS-MORE-DAYS}. PROCEED → Gate A–F pipeline. |

## First-run result (frozen audit, 2026-06-05) — KILL

Run on the paired L2 archive (`research/data/raw/<c>/*_l2.hftbt.npz`), months B6/C6/D6/E6, OOS
split 2026-04-01. **37 events across 30 event-days (57 audited days); all four contracts present**
— so this is a genuine refutation, NOT a sample wall.

| Metric | Value |
| --- | --- |
| **verdict** | **KILL** (`failed`: `median_net_non_positive`) |
| median net-after-cost (30m) | **−17 pt** |
| median **gross** return (30m) | **−9 pt** (negative even before the 8-pt cost) |
| mean net edge / trade | **−19.9 pt** |
| stop-breach rate (VWAP reclaim) | **73.7 %** |
| p10 net | −162 pt; remove-best-1 median −20.5 pt; total net −736 pt |
| net by contract | TXFB6 −198, TXFC6 −244, TXFD6 −427, TXFE6 +133 (3 of 4 negative) |

**Why it dies:** the continuation thesis is wrong-signed. A "failed VWAP reclaim" does **not**
precede trend continuation — in 73.7 % of entries the post-entry path went on to reclaim VWAP, i.e.
the imbalance mean-reverts rather than persists. The negative **gross** median means even a zero-cost,
zero-slippage version loses. This is the same reversal-dominated family as the T1-D intraday-momentum
KILL (open→close is reversal-signed). No re-optimization performed (0 of 3 used); the >10-pt edge
floor was **not relaxed**.

## Pre-registered kill/keep rule (frozen)

- **KILL** if median `net_after_cost_30m` ≤ 0, OR remove-best-1 median collapses, OR single-day
  net share > ~50 %, OR a single contract supplies all positive PnL, OR stop-breach ≥ 0.50, OR
  drawdown > 2× avg monthly net.
- **NEEDS-MORE-DAYS** if non-negative but events < 80, days < 20, or a contract is missing.
- **PROCEED** only if the full hard gate passes AND median net > 0; flag whether net clears **>10**.

Max 3 re-optimizations per `txf_led_research_discipline`; same-sample re-tuning forbidden.

## How to run the V0 audit

```bash
uv run python -m research.t1.regime_viability \
  --mode vwap_trend \
  --raw-dir research/data/raw \
  --months B6,C6,D6,E6 \
  --oos-start 2026-04-01 \
  --out-dir research/experiments/validations/t1c_vwaptrend_v0
```
