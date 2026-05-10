---
name: taifex-alpha-kill-criteria
description: Use BEFORE starting any new alpha research on TAIFEX instruments. Encodes 50+ killed alpha lessons, structural exhaustion zones, and mandatory pre-research feasibility checks to prevent wasted effort.
---

# TAIFEX Alpha Kill Criteria

Hard-won lessons from 50+ killed alphas across R6-R51. Consult this skill BEFORE investing effort in a new alpha direction to avoid repeating structurally doomed approaches.

## Pre-Research Feasibility Gate

Before writing any code, answer these 3 questions:

### Q1: Does the edge exceed the cost floor?

| Instrument | RT Cost (pts) | RT Cost (NTD) | Min Viable Signal |
|------------|--------------|---------------|-------------------|
| TMFD6 (微台指) | 4.0 | 40 | Edge > 5 pts per RT |
| TXFD6 (大台) | 0.48 | 96 | Edge > 1 pt per RT |
| TXO (台指選擇權) | ~80 pts (4.68 pts equiv) | varies | Edge > 2x spread |

If your estimated signal size is < 2x cost floor → **KILL immediately**. No amount of optimization will fix structural cost deficit.

### Q2: Is the horizon compatible with the instrument?

| Horizon | TMFD6 verdict | TXFD6 verdict | Why |
|---------|--------------|---------------|-----|
| Tick (< 1s) | EXHAUSTED | EXHAUSTED | 168 configs, 0 profitable (R14-R35) |
| Short (1s-5min) | EXHAUSTED | EXHAUSTED | OFI/MLOFI/Hawkes/lead-lag all killed |
| Medium (5min-1hr) | EXHAUSTED | EXHAUSTED | CBS 0/84 profitable; MR/momentum both dead |
| Long (1hr-1day) | UNTESTED | UNTESTED | Cost becomes negligible; viable path |
| Multi-day | UNTESTED | UNTESTED | 三大法人 flow, TSMOM, VRP potential |

### Q3: Is the alpha type structurally viable?

Check against the kill registry below before proceeding.

## Definitively Killed Alpha Classes

### Directional Signals (ALL DEAD on tick-to-hour horizon)

| Alpha class | Rounds tested | Why killed |
|-------------|--------------|------------|
| Mean-reversion (L1) | R14-R17, R20, R35 | 168 configs, 0 profitable. Adverse excursion before favorable. |
| Momentum (L1) | R14, R20, R35 | Same 168 configs. Directional ceiling: 0.001 bps. |
| OFI / MLOFI | R10-R11, R32a, R34 | IC=0.011-0.013 (true, after detrend). Cost = 1.33 bps. |
| Lead-lag (TX→TMF) | R28 | 2-day artifact. Not robust. |
| Trade-size filtering | R35 | 73% of trades qty=1. Vol-weight useless. |
| Bar-TA (KDJ/MACD) | R32b | IC inflated 7x by subsampling bug. True IC=+0.013. |
| Entropy / TDA | R36 | TDA β1 IC=+0.088 but Gate C FAIL (can't monetize). |
| Signed-flow autocorr | R8, R23 | Toxicity approved as feature [21], not tradeable signal. |
| Flow entropy | R8 | Explored, no edge. |
| Hawkes resilience | R8, R27 | R²=0.000001. |
| OIDS (order imbalance) | R17 | L1 EXHAUSTED. |

### Market-Making Improvements (ALL DEAD for R47)

| Improvement | Why killed |
|-------------|------------|
| Hawkes intensity gate | Net-negative: kills V-shape recovery days |
| GLT inventory model | Academic framework useless at 1-lot; fixed 0.2 outperforms |
| Circuit breaker stops | False positives dominate; winning days need -1,500 pt dips |
| Stale quote preservation | 0/12 days improved in CK test; fresh quotes are better |
| Vol-adaptive spread | spread=3 captures 3 pts, RT=4 pts → net -1 pt/RT |
| Time-of-day modulation | Entirely explained by spread regime, not time |
| Spread-dependent max_pos | Only 7.4% of time, stranded inventory risk |
| Cross-instrument (TMFD6→TXFD6) | TMFD6 already proven -109K NTD in multi-instrument test |

### Options (Retail Cost Wall)

| Direction | Why killed |
|-----------|------------|
| TXO skew MR | RT cost 4.68 pts > edge. Retail spread structural blocker. |
| TXO IC factor | Same cost wall. |
| TXO factor stat-arb | Same cost wall. |
| Tensor Oracle (R50) | 22 pt signal < 80 pt TXO cost. Infrastructure reusable. |

## Mandatory Signal Validation Gates

### Gate: Detrended IC

```python
# MANDATORY for any EMA-smoothed signal
ic_raw = spearman_ic(signal, forward_returns)
ic_detrended = spearman_ic(signal, forward_returns - rolling_mean_5min)

if ic_detrended < 0.01:
    KILL("trend contamination — not alpha")
if (ic_raw - ic_detrended) / ic_raw > 0.60:
    KILL("autocorrelation-inflated IC")
if ic_raw increases monotonically with horizon:
    KILL("monotonic IC = pure trend-following, not mean-reversion")
```

### Gate: Bid/Ask Execution Reality

```python
# MANDATORY for any edge < 2x median spread
entry_price = ask if buying else bid    # NOT mid
exit_price = bid if closing_long else ask  # NOT mid
pnl = exit_price - entry_price - rt_cost

# Mid-price PnL is FORBIDDEN for strategy decisions
# CBS R14: mid-price showed +3.00 bps, bid/ask showed -47.70 bps
```

### Gate: Recent Data First

```python
# Validate on most recent month FIRST
# TMFD6 spread regime: Jan-Feb (28-68 pts) → Mar (3 pts)
# Strategy profitable in Jan-Feb can be dead in March

if not validate_recent_month_first():
    KILL("recency bias — regime may have shifted")
```

### Gate: Subsampling Bias Check

```python
# R32b lesson: subsampling inflated IC 7x
# If using bars/buckets, compute IC on RAW ticks too
ic_subsampled = compute_ic(signal_bars, returns_bars)
ic_raw_ticks = compute_ic(signal_ticks, returns_ticks)

if ic_subsampled / ic_raw_ticks > 3.0:
    KILL("subsampling inflation — boundary artifact")
```

## Viable Remaining Paths (as of 2026-04-12)

| Path | Status | Prerequisite |
|------|--------|-------------|
| Daily/multi-day horizons | UNTESTED | Cost negligible at 100+ pt targets |
| TSMOM (time-series momentum) | Sharpe 0.824 validated | User decision pending |
| 三大法人 institutional flow | UNTESTED | Need daily position data pipeline |
| VRP (vol risk premium) | PARKED | Needs 6+ TXO expiry cycles |
| Night session VWAP MR | IC=-0.26 (t=-8.2) | Spread kills all execution; needs cost reduction |
| TDA β1 vol predictor | IC=+0.088, robustness PASS | Needs options deployment infrastructure |
| Institutional account | Structural enabler | Eliminates retail fee ceiling |

## Anti-Patterns

- Do NOT propose new tick-to-hour directional alphas on TAIFEX — they are structurally exhausted
- Do NOT use mid-price execution models for edge < 2x spread
- Do NOT trust EMA-smoothed IC without detrending
- Do NOT run multi-month backtests without checking recent-month regime first
- Do NOT proactively propose alpha directions — respond when user initiates (feedback rule)
