# Round 16 MC-5: Reversal Signal Persistence at Extended Horizons (XMT Cost Model)

**Date**: 2026-03-26
**Data**: TXFD6 L1 tick data, 4 days (2026-03-19 to 2026-03-24), 1,779,257 ticks
**Sampled at**: 1-second intervals (193,076 sample points)

---

## Part 1-2: Reversal Signal IC at Extended Horizons

| Horizon | Med Move | Mean Move | P25 | P75 | P95 | Imbalance IC | Imb Correct% |
|---------|----------|-----------|-----|-----|-----|-------------|-------------|
| 5s | 4.0 | 5.5 | 2.0 | 7.0 | 15.5 | 0.0446 | 52.4% |
| 10s | 5.5 | 7.8 | 2.5 | 10.0 | 21.5 | 0.0318 | 51.7% |
| 15s | 6.5 | 9.6 | 3.0 | 12.5 | 26.5 | 0.0262 | 51.4% |
| 30s | 9.5 | 13.8 | 4.5 | 18.0 | 38.0 | 0.0187 | 51.0% |
| **60s** | **13.5** | **19.8** | **6.5** | **25.5** | **54.0** | **0.0098** | **50.5%** |
| **90s** | **17.0** | **24.6** | **8.0** | **31.5** | **66.5** | **0.0074** | **50.4%** |
| **120s** | **20.0** | **28.8** | **9.0** | **37.0** | **77.5** | **0.0056** | **50.5%** |
| **180s** | **25.0** | **35.7** | **11.0** | **46.5** | **95.0** | **0.0049** | **50.4%** |
| **300s** | **32.0** | **46.8** | **14.5** | **59.5** | **126.0** | **0.0001** | **50.3%** |

**Key finding**: Imbalance IC halves every ~15 seconds. At 60s it is 0.0098 -- essentially random. By 300s, IC is 0.0001 (zero). Imbalance correctness drops from 52.4% at 5s to 50.3% at 300s.

**No sweet spot exists**: At every horizon, the accuracy-to-break-even gap is negative (accuracy < break-even). The signal decays faster than cost viability improves.

---

## Part 4: Break-Even Accuracy with XMT Costs (4.0 pts RT)

| Horizon | Median Move | Min Accuracy (median) | Min Accuracy (mean) | Viable? |
|---------|------------|----------------------|--------------------|---------|
| 5s | 4.0 pts | 100.0% | 86.1% | NO |
| 10s | 5.5 pts | 86.4% | 75.7% | YES (hard) |
| 15s | 6.5 pts | 80.8% | 70.8% | YES (hard) |
| 30s | 9.5 pts | 71.1% | 64.5% | YES |
| **60s** | **13.5 pts** | **64.8%** | **60.1%** | **GOOD** |
| **90s** | **17.0 pts** | **61.8%** | **58.1%** | **GOOD** |
| **120s** | **20.0 pts** | **60.0%** | **57.0%** | **GOOD** |
| **180s** | **25.0 pts** | **58.0%** | **55.6%** | **GOOD** |
| **300s** | **32.0 pts** | **56.2%** | **54.3%** | **GOOD** |

At XMT costs (4.0 pts RT), 60s+ horizons need only 56-65% accuracy -- but the signal provides only 50.3-50.5%. The gap is -6pp at 300s and -14pp at 60s. No horizon is viable.

---

## Part 5: Signal Decay Curve

| Horizon | Imbalance IC | IC Decay (vs 10s) | Imb Correct% |
|---------|-------------|-------------------|-------------|
| 5s | 0.0446 | 140% | 52.4% |
| 10s | 0.0318 | 100% (reference) | 51.7% |
| 15s | 0.0262 | 82% | 51.4% |
| 30s | 0.0187 | 59% | 51.0% |
| 60s | 0.0098 | 31% | 50.5% |
| 90s | 0.0074 | 23% | 50.4% |
| 120s | 0.0056 | 18% | 50.5% |

**Signal half-life**: ~15 seconds (IC drops from 0.045 to 0.019 between 5s and 30s).

---

## Part 6: Max Adverse Excursion (MAE)

Contrarian entry (bet against imbalance), measured during holding period:

| Hold Period | MAE Median | MAE P75 | MAE P90 | MAE P95 | MAE P99 | Win Rate |
|-------------|-----------|---------|---------|---------|---------|----------|
| 30s | 8.5 pts | 16.5 | 27.0 | 35.5 | 61.5 | 48.2% |
| 60s | 12.5 pts | 23.5 | 38.5 | 50.5 | 87.0 | 49.1% |
| 90s | 15.5 pts | 29.5 | 48.0 | 62.5 | 105.5 | 49.0% |
| 120s | 18.0 pts | 34.5 | 56.0 | 72.5 | 124.0 | 49.3% |
| 180s | 22.5 pts | 43.0 | 69.0 | 91.0 | 155.2 | ~49% |
| 300s | 30.0 pts | 57.5 | 90.0 | 118.0 | 198.2 | ~49% |

**Critical**: Unconditional contrarian strategy WIN RATE IS BELOW 50% at all horizons. Mean PnL is approximately zero. Raw contrarian entry is not profitable.

**MAE scaling**: MAE grows roughly as sqrt(time). At 300s, P95 MAE = 118 pts = 29.5x the RT cost. Significant inventory risk.

**MAE risk**: At 60s hold, median adverse excursion = 12.5 pts (3x the XMT cost), P95 = 50.5 pts. Significant inventory risk.

---

## Bonus: Toxic Flow Filter and Conditional Analysis

### Does imbalance STRENGTH help?

| Horizon | Imb Strength | N | Imb Correct% | Reversal% |
|---------|-------------|---|-------------|----------|
| 5s | weak (0-0.33) | 34,217 | 50.9% | 49.1% |
| 5s | medium (0.33-0.67) | 97,586 | 52.9% | 47.1% |
| 5s | strong (0.67-1.0) | 3,306 | 53.4% | 46.6% |
| 60s | weak | 35,650 | 49.5% | **50.5%** |
| 60s | medium | 101,591 | 50.8% | 49.2% |
| 60s | strong | 3,467 | 51.6% | 48.4% |

Stronger imbalance slightly improves prediction (53.4% at 5s for strong) but not enough. At 60s, even weak imbalance is barely above random.

### Does opposing OFI predict reversals?

| Horizon | Condition | N | Reversal% |
|---------|-----------|---|----------|
| 5s | All | 135,109 | 47.6% |
| 5s | OFI opposing imbalance | 58,790 | 48.5% |
| 5s | OFI aligned with imbalance | 72,144 | 46.9% |
| 60s | All | 140,708 | 49.5% |
| 60s | OFI opposing imbalance | 61,080 | 49.7% |
| 60s | OFI aligned with imbalance | 75,004 | 49.2% |

Opposing OFI provides marginal lift (+0.9% at 5s, +0.5% at 60s). Not actionable.

### Combined filter (strong imbalance + opposing OFI)?

| Horizon | N | Reversal% | Med Move |
|---------|---|----------|----------|
| 5s | 1,710 | 50.5% | 4.5 |
| 10s | 1,747 | 49.8% | 6.0 |
| 30s | 1,769 | 48.8% | 10.5 |
| 60s | 1,786 | 50.5% | 15.0 |

**Combined filter has NO predictive power** (reversal rate ~50% = random).

---

## MC-5 Verdict

### The Hard Truth

The raw depth imbalance signal on TXFD6 L1 data has:
- **IC of only 0.045 at 5s**, decaying to 0.01 at 60s
- **Prediction accuracy of only 52.4% at best** (5s horizon)
- **Unconditional contrarian win rate below 50%** at all horizons 30s+
- **No meaningful improvement from toxic flow filtering or OFI conditioning**

### Why This Differs from the Literature

Albers et al. (2025) found profitable reversals on Binance BTC perpetual because:
1. BTC has **maker rebates** (~0.01% rebate) -- we have zero
2. BTC has **much higher volatility** (larger moves per tick)
3. Their reversal signal used **15+ engineered features** (return autocovariance, top-of-book survival time, volatility), not just raw imbalance
4. Their strategy was **slightly profitable with minimum-sized orders** and "likely not scalable"

Our analysis used only L1 imbalance, which is the crudest possible reversal detector. The Albers et al. features (return autocovariance over 5s windows, 100ms price drop indicators, inter-trade time) are not computable from our L1 snapshot data -- they require trade-by-trade records.

### Feasibility Assessment

| Condition | Threshold | Observed | Status |
|-----------|-----------|----------|--------|
| Break-even accuracy (60s, XMT) | 64.8% | 50.5% raw | **GAP: 14.3pp** |
| Signal IC at 60s | meaningful | 0.0098 | **WEAK** |
| Raw contrarian win rate | >50% | 48-49% | **BELOW BREAK-EVEN** |
| Toxic flow filter improvement | meaningful | +0.5% | **NEGLIGIBLE** |

### Recommendation

1. **Raw imbalance reversal at 60s+**: NOT VIABLE with L1 data alone. The 14.3 percentage point gap between required (64.8%) and observed (50.5%) accuracy cannot be bridged by filtering.

2. **At 5-10s horizon with richer features**: POSSIBLY VIABLE but requires:
   - Trade-by-trade data (not just L1 snapshots)
   - Return autocovariance, inter-trade timing, TOB survival features
   - XMT costs make 5s impossible (median move < cost); 10s marginal

3. **For XMT (4.0 pts RT)**: Need 60s+ holding period for cost viability, but signal decays to noise by 60s. This is a fundamental tension -- **the cost-viable horizon is beyond the signal horizon**.

4. **Path forward**: Either (a) reduce costs (TXFD6 actual costs may be lower than XMT), (b) find much stronger features than raw L1 imbalance, or (c) accept this direction is unviable for XMT.
