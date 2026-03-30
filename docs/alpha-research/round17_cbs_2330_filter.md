# Round 17 Direction B: CBS + 2330 Confirmation Filter

**Date**: 2026-03-26
**Script**: `research/experiments/validations/tsmc_leadlag/cbs_2330_filter.py`
**Data**: 22 days, 175 CBS triggers

---

## Summary

The 2330 confirmation filter **improves CBS trade quality by ~4.3 bps per trade**, primarily by filtering out losing trades. However, the overall CBS strategy is negative on this dataset (-2.59 bps net per trade), so the filter turns a losing strategy into a breakeven one rather than a profitable one.

---

## CBS Parameters Used

| Parameter | Value |
|-----------|-------|
| Move threshold | 40 bps in 600s |
| Hold period | 300s |
| Stop loss | 15 bps |
| Min gap between triggers | 600s |
| RT cost | 1.33 bps |

---

## Results: ALL vs Confirmed vs Denied

| Metric | ALL (n=175) | CONFIRMED (n=74) | DENIED (n=101) |
|--------|-------------|-------------------|----------------|
| Mean raw PnL | -1.26 bps | **+1.23 bps** | -3.09 bps |
| Mean net PnL | -2.59 bps | **-0.10 bps** | **-4.42 bps** |
| Median net PnL | -9.38 bps | -0.43 bps | -16.35 bps |
| Win rate | 40.6% | **45.9%** | 36.6% |
| Stop rate | 46.3% | **39.2%** | 51.5% |
| t-stat vs 0 | -1.86 (p=0.065) | -0.04 (p=0.967) | **-2.65 (p=0.009)** |

### Interpretation

- **Denied trades are significantly unprofitable** (p=0.009). When TSMC is still trending in the direction of the move, the contrarian CBS entry loses money. This makes sense: if the large-cap stock is still moving, the index move is structural/informed, not a transient dislocation.

- **Confirmed trades are breakeven** (p=0.967). When TSMC has already reversed (agrees with contrarian), the CBS entry is roughly flat. The filter removes the worst trades but doesn't create positive alpha.

- **The filter splits trades ~42/58**: 74 confirmed, 101 denied. Most CBS triggers are "denied" because TSMC momentum typically aligns with the original move (momentum continuation is the norm).

### Confirmed vs Denied Statistical Test

| Metric | Value |
|--------|-------|
| Confirmed - Denied | +4.33 bps |
| t-stat | 1.54 (p=0.126) |
| Win rate improvement | +9.3 pp (45.9% vs 36.6%) |
| Stop rate improvement | -12.3 pp (39.2% vs 51.5%) |

The difference is economically meaningful (+4.33 bps) but not statistically significant at p<0.05 level. With 175 trades and high variance (std ~18 bps), power is limited.

---

## Key Concern: CBS is Net Negative on This Dataset

The overall CBS strategy shows -2.59 bps net per trade across 22 days. This differs from the original CBS OOS result (+3.00 bps on TMF). Possible explanations:

1. **Different dataset period**: Original CBS was validated on different dates
2. **Contract mismatch**: Some dates use TMFB6/TMFC6 instead of TMFD6
3. **Parameter sensitivity**: The 40 bps / 600s / 300s hold may not be optimal for all contracts
4. **Stop loss impact**: 46% stop rate suggests the 15 bps stop is too tight for this data

The 2330 filter's value is RELATIVE: it makes bad trades less bad. If CBS parameters are re-optimized, the filter could turn positive trades into better trades.

---

## Verdict

**The 2330 confirmation filter is directionally useful but not sufficient.**

- It correctly identifies which CBS trades are more likely to fail (TSMC still trending = informed move = don't fade it)
- The +4.33 bps improvement is meaningful (3.25x the RT cost)
- But it turns a -2.59 bps strategy into -0.10 bps, not into a profitable one

**Recommendation**: Use as a defensive filter (reject CBS entries where 2330 disagrees), but the primary alpha must come from CBS itself being positive. The filter is a risk reduction tool, not an alpha generator.
