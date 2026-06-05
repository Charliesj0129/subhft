# T1-F — TXF Expiration V-Reversal → TMF

**Track:** T1 (TXF higher-timeframe regime → TMF expression). See `track_t1_opened_2026_05_13`.
**Status:** V0 viability audit (Stage-1 hard gate). **NEEDS-MORE-DAYS** (structural sample blocker — see below). NOT promotion-eligible. No live wiring exists.
**Created:** 2026-06-05.
**Origin:** User-initiated hypothesis (H3 from the 2026-06-03 paper×data menu), chosen after the
**T1-D intraday-momentum KILL** (open→close is reversal-signed) and the **T1-E open-gap-fade KILL**
(overnight gaps *continue*, 90.6% stop-breach). H3 isolates the one session where index-futures
microstructure is most distorted — the **final settlement day** — where basis convergence and
arbitrage unwinding are documented to produce sharp directional thrusts that partially mean-revert
("expiration-day reversal" / pinning literature; Stoll & Whaley triple-witching reversal effects).

This is the fixed pre-registered spec. It is **frozen** before the first run: no parameter search,
no same-day distribution thresholds, no post-hoc rule edits (per `txf_led_research_discipline`
rules 7, 12, 19).

| Field | Value |
| --- | --- |
| **strategy_name** | `t1f_txf_expiration_vreversal_tmf` |
| **market** | TAIFEX futures, **settlement day only** (3rd Wednesday, day session 08:45–13:30 Asia/Taipei). |
| **instrument** | Signal = TXF (TXFB6/C6/D6/E6 front quarter). Execution = TMF. TXF→TMF, single-leg directional. |
| **hypothesis** | On the final settlement day, an outsized open→early-session thrust partially reverts (V/inverted-V). Fade it: thrust up → short, thrust down → long. Edge = settlement-day overreaction / partial reversal, **not** L2 lead–lag and **not** continuation. |
| **timeframe** | Higher-timeframe session structure: open vs the +90-min thrust-window mid on the settlement day. L2 only for executable bid/ask + quote sanity. |
| **holding_period** | One trade per settlement day. Enter ~10:15 (open + 90-min thrust window); hold to the 30-min headline horizon (also 15/60-min). |
| **entry_rule** | `displacement = TXF mid(open+90min) − TXF open mid` (endogenous). Require `\|displacement\| ≥ 20 TXF pts`. Direction = `−sign(displacement)` (fade). Enter at the thrust-window end. TMF entry = executable **ask** (long) / **bid** (short). |
| **exit_rule** | Time-based exit at 15/30/60-min horizons on the executable opposite side. Stop structure = thrust **extends** past its in-window extreme by a 15-pt buffer (continuation against the fade); `stop_structure_breached` recorded when the post-entry path touches it. |
| **position_sizing** | Fixed 1 lot per event (V0). No scaling/pyramiding/\|pos\|-gating. |
| **risk_control** | One entry per settlement day; executable bid/ask only (no mid fills); thrust-continuation stop (15 pt); invalid-quote guard via BBO reconstruction (bid<ask, qty>0); session-bounded. HALT/force-flat inherited from platform at any future live stage. |
| **cost_model** | TMF executable bid/ask captures the spread; **8 pt round-trip** fee+tax+slippage on top per `txf_led_research_discipline` (`feedback_taifex_fee_structure`). `net_after_cost = gross_executable_return − 8`. TMF point value 10 NTD/pt. Latency (P99 ~500 ms) NOT applied at V0 — parity with T1-A/B/D/E V0; deferred. |
| **validation_plan** | Stage-1 V0 hard gate: ≥20 trading days, ≥80 events, B6/C6/D6/E6 all present, executable bid/ask, 8-pt cost, median net > 0, p10 not catastrophic, remove-best-1 ≥0, stop-breach < 0.50, no single-contract concentration, no single-day-dominance, drawdown ≤ 2× avg monthly net. Verdict ∈ {PROCEED, KILL, NEEDS-MORE-DAYS}. PROCEED → Gate A–F pipeline. |

## Structural sample blocker (the binding constraint)

This signal fires **once per contract per month** — there is exactly one settlement day per delivery
month. The V0 hard gate's ≥20-trading-day / ≥80-event floor is therefore bounded not by detector
design but by **how many monthly settlements the paired L2 dataset spans**. In the current archive:

| Contract | Settlement (3rd Wed) | In paired data? |
| --- | --- | --- |
| TXFB6 | 2026-02-18 | **No** — falls in the B6 recorder gap (Feb 06 → Feb 23). |
| TXFC6 | 2026-03-18 | **Yes** |
| TXFD6 | 2026-04-15 | **Yes** |
| TXFE6 | 2026-05-20 | **No** — beyond data end (2026-05-08). |
| TXFF6+ | ≥ 2026-06-17 | **No** — beyond data end. |

→ **2 usable settlement days.** Reaching the 20-day floor would require ~18 more monthly settlements
(≈ 18 months of paired TXF/TMF L2). The floor is **NOT relaxed** to accommodate the small sample
(per `不足樣本不得完成`).

## First-run result (frozen audit, 2026-06-05)

- **Verdict: NEEDS-MORE-DAYS** (`needs_more_sample`: events 2 < 80, days 2 < 20, B6/E6 absent).
- Both settlement events were **positive net** (C6 +65 pt, D6 +25 pt; mean net +45, stop-breach 0%).
  C6 thrust −93 pt → faded long; D6 thrust +279 pt → faded short. The fade was **not refuted** on
  these 2 days — unlike T1-D/T1-E, the direction is the right sign. But N=2 is anecdote, not edge:
  the candidate is **un-auditable**, not promising. No re-optimization performed (0 of 3 used).

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
  --mode expiration_v_reversal \
  --raw-dir research/data/raw \
  --months B6,C6,D6,E6 \
  --oos-start 2026-04-01 \
  --out-dir research/experiments/validations/t1f_expiration_vreversal_v0
```

As more monthly settlements are exported into `research/data/raw`, re-running the same frozen
command extends the sample automatically — the verdict will flip from NEEDS-MORE-DAYS to KILL or
PROCEED on its own once the floor is cleared.
