# Round 16: Push-Response Validation on TMFD6

**Date**: 2026-03-26
**Data**: TMFD6 combined Jan/Feb + March, 6,997,542 ticks (~58 days)
**Method**: 100-tick backward return (push), forward return at various lags (response)
**Cost threshold**: 4.0 pts XMT round-trip

---

## Raw Results (All Data Combined)

### 3σ Negative Push (< -35.5 pts in 100 ticks), De-duplicated (761 events, 13.1/day)

| Lag | ~Seconds | Mean Response | Median | Trimmed Mean (5-95%) | % > 0 | % > 4 pts |
|-----|----------|--------------|--------|---------------------|-------|-----------|
| 50 | 28s | +14.42 | 0.0 | -0.77 | 36.3% | 31.3% |
| 100 | 56s | +33.34 | 0.0 | +0.31 | 46.0% | 41.4% |
| 200 | 111s | +45.00 | 0.0 | +7.33 | 49.5% | 46.6% |
| 500 | 278s | +35.31 | +1.0 | -2.33 | 50.3% | 46.8% |
| 1000 | 556s | +45.57 | 0.0 | +2.96 | 48.6% | 41.9% |

Means are large (+14 to +45 pts) but medians are near zero and trimmed means are near zero. The mean is driven by heavy right-tail outliers.

### Asymmetry Check: 3σ Positive Push (873 events, 15.1/day)

| Lag | Mean Response | Median | % < 0 (reversion) |
|-----|--------------|--------|-------------------|
| 50 | +4.36 | 0.0 | 20.2% |
| 100 | +9.64 | 0.0 | 25.7% |
| 200 | +24.17 | +1.5 | 28.2% |
| 500 | +47.84 | +7.0 | 37.2% |

**Positive pushes do NOT revert.** Response is POSITIVE (continuation/momentum), not negative. This is the OPPOSITE of mean-reversion.

---

## CRITICAL FINDING: Regime Split

### Jan/Feb (Wide Spread Regime) -- 402 events

| Lag | Mean Response | Median | % > 4 pts |
|-----|--------------|--------|-----------|
| 100 | **+81.6** | **+5.0** | 50% |
| 200 | **+116.6** | **+26.0** | 61% |
| 500 | **+126.6** | **+41.5** | 64% |

**Strong reversion**: After large drops, price bounces back substantially. Median reversion of +26 pts at 200 ticks (111s). 61% of events exceed 4 pts.

### March (Tight Spread Regime) -- 359 events

| Lag | Mean Response | Median | % > 4 pts |
|-----|--------------|--------|-----------|
| 100 | **-20.7** | **-7.5** | 31% |
| 200 | **-35.2** | **-16.5** | 30% |
| 500 | **-66.9** | **-49.0** | 27% |

**Momentum, NOT reversion**: After large drops, price CONTINUES falling. Median loss of -16.5 pts at 200 ticks. Only 30% exceed +4 pts.

---

## Interpretation

The push-response pattern is **entirely regime-dependent**:

1. **Jan/Feb (wide spread)**: Large drops are temporary liquidity events. Spreads are wide because the market is thin. Large price moves are caused by temporary order flow imbalance, not genuine information. The bid side replenishes, and prices revert. Mean reversion of +82 to +127 pts on 3σ events.

2. **March (tight spread)**: Large drops are INFORMATIONAL. The market is liquid and efficient. Large moves represent genuine price discovery. No reversion -- prices continue in the direction of the initial move (momentum). Mean response of -21 to -67 pts.

3. **Positive pushes**: Show continuation in both regimes. The asymmetry (negative reverts, positive continues) exists only in the wide-spread regime. In March, both directions show momentum.

## Pass/Fail Assessment

| Configuration | Mean Response | vs 4.0 pt threshold | Regime |
|--------------|--------------|---------------------|--------|
| Jan/Feb, 3σ neg, 200-tick | +116.6 pts | **PASS (29x)** | Wide spread only |
| Jan/Feb, 3σ neg, 100-tick | +81.6 pts | **PASS (20x)** | Wide spread only |
| March, 3σ neg, 200-tick | -35.2 pts | **FAIL** | Tight spread |
| All data combined (misleading) | +45.0 pts | "PASS" | Artifact of regime mixing |

**CONDITIONAL PASS**: The push-response signal PASSES in the Jan/Feb wide-spread regime (+82 to +127 pts mean reversion, 13 events/day, 50-64% positive). It FAILS in the March tight-spread regime (momentum, not reversion).

## Caveats

1. **Trimmed means are near zero**: The large means are driven by extreme right-tail events. Median response is 0 in combined data and +5 to +42 only in Jan/Feb.
2. **Only 402 de-duplicated events in Jan/Feb** (13/day): Limited sample for validation.
3. **January/February market conditions may not recur**: The wide-spread regime was specific to that period (possibly contract rollover, Lunar New Year, or macro event). If tight spreads persist, the signal is useless.
4. **Positive pushes show continuation, not reversion**: The asymmetry is regime-specific, not structural. In March, BOTH directions show momentum.
5. **High variance**: Even in Jan/Feb, P25 of response at 200 ticks is -8 pts. Significant loss risk on individual trades.

## Recommendation

The push-response signal is a **regime-conditioned opportunity**, not a standalone strategy. It works WHEN and ONLY WHEN spreads are wide (Jan/Feb regime). The key dependency is spread regime detection, which loops back to our earlier Candidate C (Spread Regime Prediction).

**Next step**: Determine whether the wide-spread regime is (a) a recurring feature of TMFD6 or (b) a one-time anomaly from Jan/Feb 2026. This requires longer historical data than our 58 days.
