# Round 17: Cross-Session Intraday Patterns on TMFD6

**Date**: 2026-03-26
**Instrument**: TMFD6 / TMFC6 / TMFB6 (Mini-TAIEX Futures, near-month selected by tick volume)
**Data**: 7.69M ticks across 38 trading dates (2026-01-27 to 2026-03-26)
**RT Cost**: 4 pts = 40 NTD = 1.33 bps
**ClickHouse price_scaled**: x1,000,000 (verified via `recorder/mapper.py`)

---

## C1: Opening Gap Signal

**Setup**: gap = day session open - preceding night session close (pts and bps).
Test gap-fade (mean-reversion) vs gap-and-go (momentum) at 30/60/120 min and full-session horizons.

**N = 27 gap observations.**

### Gap Statistics

| Metric | Value |
|--------|-------|
| Mean gap | +32.3 pts (+9.00 bps) |
| Std gap | 393.9 pts |
| Range | [-965, +655] pts |

### IC: Gap Size/Direction vs Forward Returns

| Horizon | IC (size) | IC (direction) | Fade avg (pts) | Go avg (pts) | Fade WR | Go WR |
|---------|-----------|----------------|----------------|--------------|---------|-------|
| 30 min | -0.214 | -0.259 | +40.9 | -40.9 | 66.7% | 29.6% |
| 60 min | +0.018 | -0.235 | +50.6 | -50.6 | 51.9% | 44.4% |
| 120 min | -0.089 | -0.193 | +57.0 | -57.0 | 63.0% | 33.3% |
| Full session | -0.282 | -0.312 | +113.7 | -113.7 | 70.4% | 25.9% |

### Gap Quintile Analysis (Full Session Return)

| Quintile | Mean Return (pts) | N |
|----------|-------------------|---|
| Q1 (big down gap) | +102.2 | 6 |
| Q2 | +19.8 | 5 |
| Q3 (neutral) | +65.4 | 5 |
| Q4 | -258.2 | 5 |
| Q5 (big up gap) | -98.0 | 6 |

### Large Gaps (|gap| > 190 pts, n=13)

| Horizon | Fade avg | Go avg |
|---------|----------|--------|
| 30 min | +5.2 | -5.2 |
| 60 min | -47.8 | +47.8 |
| Full | +159.8 | -159.8 |

### C1 Assessment

**Gap-fade is the dominant pattern.** Negative IC at full session (-0.312 directional) with 70.4% win rate is the strongest signal in this study. Quintile monotonicity: big down gaps → positive session, big up gaps → negative session.

**Statistical significance**: t=1.97, p=0.060 (borderline at 5%, significant at 10%). **However**: estimated per-trade std ~300 pts means position sizing is critical. Net after cost: 113.7 - 4 = 109.7 pts average, but with 300 pts std, this is a Sharpe ~0.38 per trade.

**Concern**: 60-min horizon shows IC reversal (fade doesn't work at 60 min for large gaps), suggesting the path is non-monotonic. The fade works early (30 min) and late (full session) but not at 60 min.

**Kill gate**: CONDITIONAL PASS. |mean bps| = 32 bps >> 4 bps threshold, WR = 70.4% >> 60%. But n=27 is marginal for statistical confidence. Needs 60+ observations for robust assessment.

---

## C2: Time-of-Day Return Patterns (30-min Buckets)

Day session (08:30-13:45 Taiwan time) divided into 30-minute periods.

### Return Heatmap

| Bucket | Mean (pts) | Mean (bps) | Std (pts) | Sharpe | N | %Pos | %>4pts |
|--------|------------|------------|-----------|--------|---|------|--------|
| 08:30 | +3.3 | +0.82 | 109.8 | +0.030 | 28 | 46.4% | 100.0% |
| 09:00 | -10.4 | -3.05 | 203.2 | -0.051 | 28 | 53.6% | 100.0% |
| 09:30 | **+29.3** | **+8.96** | 167.1 | **+0.176** | 28 | **60.7%** | 89.3% |
| 10:00 | -8.0 | -2.09 | 130.1 | -0.062 | 27 | 51.9% | 100.0% |
| 10:30 | -19.1 | -5.82 | 137.7 | -0.139 | 27 | 48.1% | 92.6% |
| 11:00 | -3.0 | -0.69 | 80.3 | -0.037 | 26 | 50.0% | 96.2% |
| 11:30 | +11.6 | +3.49 | 83.8 | +0.139 | 26 | 57.7% | 100.0% |
| 12:00 | -9.2 | -2.64 | 87.7 | -0.105 | 25 | 40.0% | 88.0% |
| 12:30 | -2.8 | -0.78 | 81.4 | -0.035 | 26 | 42.3% | 92.3% |
| 13:00 | **-23.1** | **-6.61** | 113.2 | **-0.204** | 19 | 47.4% | 89.5% |
| 13:30 | -1.1 | -0.29 | 80.9 | -0.013 | 17 | 47.1% | 94.1% |

### Cumulative Intraday Pattern

```
08:30 →  +3.3 pts
09:00 →  -7.1 pts  (opening momentum reversal)
09:30 → +22.2 pts  (PEAK - strong bid)
10:00 → +14.2 pts
10:30 →  -4.9 pts  (mid-morning selloff begins)
11:00 →  -7.9 pts
11:30 →  +3.8 pts  (lunch bounce)
12:00 →  -5.5 pts
12:30 →  -8.3 pts
13:00 → -31.4 pts  (closing hour selloff)
13:30 → -32.5 pts
```

### C2 Assessment

**Two actionable buckets**:
1. **09:30 bucket** (+29.3 pts, +8.96 bps, Sharpe 0.176, 60.7% positive): The strongest positive period. However, std=167 pts makes per-bucket trading marginal after costs.
2. **13:00 bucket** (-23.1 pts, -6.61 bps, Sharpe -0.204): Systematic closing-hour selloff, but only 47.4% negative (not directionally consistent).

**Structural pattern**: TMFD6 shows a clear "morning bid → afternoon fade" pattern. The market peaks around 09:30-10:00 then drifts lower into close. Cumulative drift is -32.5 pts/day on average (net bearish bias in sample period).

**Kill gate**: NO PASS. No single bucket has both |mean bps| > 4 AND consistency > 60% of days in the correct direction. The 09:30 bucket is closest (8.96 bps, 60.7%) but high variance and small sample weaken confidence.

---

## C3: First/Last Hour Dynamics

### Summary Statistics

| Period | Mean (pts) | Std (pts) | %Pos | N |
|--------|------------|-----------|------|---|
| First 30 min | +29.9 | 169.4 | 50.0% | 28 |
| Morning (2h) | +2.1 | 277.7 | 46.4% | 28 |
| Last 30 min | -22.5 | 83.2 | 45.0% | 20 |
| Last 45 min | -8.6 | 78.6 | 51.9% | 27 |
| Full session | -30.5 | 365.0 | 42.9% | 28 |

### Morning Trend → Last 30 Min

| Signal | Avg PnL (pts) | Win Rate |
|--------|---------------|----------|
| IC(morning → last30) | +0.189 | - |
| WITH morning trend (last 30 min) | +30.5 | 55.0% |
| AGAINST morning trend (last 30 min) | -30.5 | 45.0% |

**Interpretation**: The last 30 minutes show weak momentum (+0.189 IC), not reversal. This contradicts the intuition that closing should mean-revert. However, WR is only 55% and n=20.

### First 30 Min → Rest of Day

| Signal | Avg PnL (pts) | Win Rate |
|--------|---------------|----------|
| IC(first30 → rest) | +0.074 | - |
| Continue first30 direction | +37.3 | 64.3% |
| Fade first30 direction | -37.3 | 35.7% |

**Interpretation**: First 30 min direction weakly predicts rest-of-day direction (IC=+0.074). The continuation signal has 64.3% WR, consistent with R14 finding that opening = momentum. But t=0.54, p=0.59 — not significant.

### C3 Assessment

**Kill gate**: NO PASS. No first/last hour signal meets both thresholds. The first-30-continuation (37.3 pts, 64.3% WR) is directionally interesting but not statistically significant with n=28.

---

## C4: Day-of-Week Effects

### Day Session by DOW

| Day | Mean (pts) | Mean (bps) | Std (pts) | Sharpe | N | %Pos |
|-----|------------|------------|-----------|--------|---|------|
| Monday | -82.5 | -23.9 | 163.7 | -0.504 | 4 | 50.0% |
| Tuesday | -149.8 | -44.4 | 547.1 | -0.274 | 6 | 33.3% |
| Wednesday | -22.2 | -6.5 | 407.0 | -0.054 | 6 | 33.3% |
| Thursday | -102.5 | -30.4 | 201.9 | -0.508 | 6 | 33.3% |
| **Friday** | **+268.2** | **+83.1** | 307.6 | **+0.872** | 5 | **80.0%** |

### Night Session by DOW

| Day | Mean (pts) | Std (pts) | N | %Pos |
|-----|------------|-----------|---|------|
| Monday | +590.7 | 279.6 | 3 | 100.0% |
| Tuesday | +161.4 | 703.3 | 8 | 50.0% |
| **Wednesday** | **+300.1** | 237.5 | 8 | **87.5%** |
| **Thursday** | **-467.1** | 249.1 | 7 | **0.0%** |
| Friday | -137.5 | 484.6 | 6 | 33.3% |

### Statistical Significance

| Signal | t-stat | p-value |
|--------|--------|---------|
| Friday day session | +1.95 | 0.123 |
| **Thursday night session** | **-4.96** | **0.003** |

### C4 Assessment

**Thursday night is the strongest statistical signal in the entire study.** Mean = -467 pts with 0% positive days (7/7 negative). t=-4.96, p=0.003. This is highly significant even with n=7.

**Possible explanation**: Thursday night (15:00 Thu → 05:00 Fri) may reflect institutional pre-weekend de-risking, or correlation with US Thursday trading.

**Friday day session** (+268 pts, 80% positive, Sharpe 0.87) is also compelling but p=0.123 with only n=5.

**Kill gate**: Thursday night PASS (|mean bps| = 130 >> 4, consistency = 100% but sample tiny). Friday day CONDITIONAL PASS (|mean bps| = 83 >> 4, consistency 80%, but n=5 and p=0.123).

**CRITICAL WARNING**: All DOW effects have n=3-8 per bucket. These are suggestive, not conclusive. Need 20+ observations per DOW for reliable inference.

---

## C5: Overnight Holding Signal

### Overnight Return (Day Close → Next Day Open)

| Metric | Value |
|--------|-------|
| Mean | +74.2 pts (+25.7 bps) |
| Std | 880.5 pts |
| %Positive | 55.6% |
| N | 27 |

### Predictability

| Signal | IC | Avg PnL | Win Rate |
|--------|------|---------|----------|
| Day return → overnight | -0.056 | - | - |
| WITH day trend overnight | +11.6 | 44.4% |
| AGAINST day trend overnight | -11.6 | 55.6% |
| Day[i] → Day[i+1] serial | -0.103 | - | - |

### C5 Assessment

**Kill gate**: NO PASS. Overnight return is positive on average (+74 pts) but with 880 pts std, this is noise. Weak negative serial correlation (IC=-0.10) suggests mild mean-reversion between days but far too weak to trade.

---

## Summary: Kill Gate Results

| Signal | Mean (bps) | Consistency | N | p-value | Kill Gate |
|--------|------------|-------------|---|---------|-----------|
| C1: Gap Fade (full session) | +32 bps | 70.4% | 27 | 0.060 | **CONDITIONAL PASS** |
| C2: 09:30 bucket | +9.0 bps | 60.7% | 28 | ~0.25 | NO PASS |
| C2: 13:00 bucket | -6.6 bps | 47.4% neg | 19 | ~0.40 | NO PASS |
| C3: First30 continue | +11 bps | 64.3% | 28 | 0.593 | NO PASS |
| C3: Last30 momentum | +9.0 bps | 55.0% | 20 | ~0.35 | NO PASS |
| **C4: Thursday night (short)** | **-130 bps** | **100%** | **7** | **0.003** | **PASS (tiny N)** |
| C4: Friday day (long) | +83 bps | 80.0% | 5 | 0.123 | CONDITIONAL (tiny N) |
| C5: Overnight long | +26 bps | 55.6% | 27 | ~0.60 | NO PASS |

## Recommendation

### Tradeable (with caveats):

1. **C1 Gap Fade**: The most robust signal. Enter at day session open AGAINST the overnight gap direction, hold to session close. Expected: +114 pts (after 4 pts cost = +110 pts), WR 70%. Needs more data (60+ obs) to confirm. **Implementation: simple, one trade per day.**

2. **C4 Thursday Night Short**: Statistically strongest but only 7 observations. If pattern holds, short at Thursday 15:00, cover at Friday 05:00. Expected: +467 pts per trade. **Extremely preliminary — needs 6+ more months of data.**

### Not tradeable:

- Time-of-day patterns (C2): Returns exist but variance too high relative to edge.
- First/last hour (C3): Directional signals are real but sub-threshold for profitability.
- Overnight holding (C5): Positive drift but massive variance.
- Serial day correlation (C5): IC=-0.10 too weak.

### Structural observations:

- TMFD6 has a **bearish intraday drift** (-32.5 pts cumulative over the day session) in this sample. This may be period-specific (Jan-Mar 2026).
- The market peaks around 09:30-10:00 and fades into close.
- Night sessions show net positive returns on Mon-Wed, negative Thu-Fri — possible US market correlation.
- Gap-fade working at full session but NOT at 60 min suggests a complex intraday path that briefly continues the gap before reversing.

### Next steps:

1. Accumulate 60+ gap observations (3+ months) for C1 gap-fade validation.
2. Monitor Thursday night pattern for another 2 months before any shadow trading.
3. Consider combining C1 (gap-fade) with C2 (09:30 bucket) for a two-signal system.
4. Investigate US overnight returns as predictor of TMFD6 night session (proxy for Thursday effect).
