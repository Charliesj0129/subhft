# H-R47-EXIT: does a bounded-loss exit improve r47_tmf_v1?

Status: **hypothesis, T0** (opened 2026-10-11 by Charlie's decision, "開研究流程"). No code, no config,
no change to the frozen live registry (loop_v1 L11). `r47_tmf_v1` stays as is; this note only
decides what is worth testing.

## Observation (from the 2026-10-06 post-deploy review)

`src/hft_platform/strategies/r47_maker.py:961-983`: the spread, toxicity and PE gates stop `_generate_quotes` as a
whole, including the quote that would unwind a held lot. With a position held, 373 of 388 gate
samples were `blocked_by=spread`. The strategy has no exit by holding time or by loss. The largest
single SIM loss on 10/5 was -165 pts (-1,650 NTD), held 1.9 h, and it alone triggered StormGuard STORM.

## What the data does and does not say

Preliminary only. Source: `hft.fills` of `R47_MAKER_TMF` (SIM, 2026-08-21 to 2026-10-09), naive
alternating entry/exit pairing at max_pos=1, same symbol only, |pnl| < 300 pts and hold < 6 h
(165 of 1,730 pairs dropped as roll/mismatch artefacts). **SIM fills are not edge evidence**
(see memory `shioaji_sim_matching_rules`); this table only tells where tail losses sit.

| hold time | pairs | sum pts | mean pts | worst pts |
|---|---|---|---|---|
| < 60 s | 620 | 2,532 | 4.1 | -200 |
| 1-5 min | 507 | 4,934 | 9.7 | -284 |
| 5-30 min | 357 | 1,839 | 5.2 | -294 |
| 30 min-2 h | 64 | 603 | 9.4 | -272 |
| 2-6 h | 17 | 8 | 0.5 | -216 |

Two things follow, and one earlier claim is withdrawn:
- Tail losses of -200 to -294 pts occur in **every** hold bucket, so a *time* exit alone does not
  remove them. The candidate lever is a **loss-bounded** exit, not a timer.
- 10/6's statement that "positions held > 30 min were 39% of the loss" came from one day (4 trades) and
  is **not supported** by the 56-day pairing above (> 30 min nets slightly positive). Withdrawn.
- 2-6 h holds have a mean near zero: if there is a time effect, it starts after about 2 h.

## Hypothesis

H1. Adding an exit that flattens a held lot once unrealised loss reaches L points (or the quote is gated
for longer than T seconds) cuts the left tail of per-trade PnL by more than it costs in the
trades that would have recovered, **net of the retail cost of crossing the spread** (TMF RT ~ 4 NTD fee
plus tax; the exit pays the spread, the maker entry does not).

H0. The recovered trades pay for the tail: net PnL after costs is unchanged or worse.

## Test design (all offline; nothing touches live)

1. Replay the recorded L1-L5 books with the unified backtest framework
   (`unified_backtest_framework`), baseline = current R47 logic; state which backtest method is used
   (`backtest_method_reliability`: a claim without a method is void).
2. Grid L in {40, 60, 80, 120} pts and T in {off, 300 s, 900 s}; exit executes at bid/ask (edge < 2x spread
   rule), with the Shioaji P95 cancel/place latency (`config/research/latency_profiles.yaml`).
3. Day-axis statistics only (`day_axis_gates_counted_rows_not_days`): per-day PnL, BH correction over
   the 12 grid cells, effective test count stated.
4. Recency check first (`feedback_backtest_recency_bias`): last 20 days reported separately.

## Kill criteria (pre-registered, not to be relaxed)

KILL if, on days, any of: mean net PnL/day change < 0 at the best cell after BH correction; max
single-trade loss not reduced by at least 30%; improvement present only in the first half of the sample.
NEEDS-MORE-DAYS if the sign is positive but fewer than 30 eligible days support it.
A pass only makes R47-EXIT a Gate-C candidate; promotion stays blocked by the L11 freeze.

## Not in scope

The StormGuard drawdown denominator (decided separately: absolute NTD, PR #568); the 8 Hz / SIM matching
questions; any live change.
