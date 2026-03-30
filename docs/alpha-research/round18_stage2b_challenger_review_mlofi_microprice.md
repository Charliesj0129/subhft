# Round 18 Stage 2b Challenger Review: MLOFI Microprice Correction

**Date**: 2026-03-27
**Reviewer**: Challenger (Stage 2b)
**Artifact**: `round18_stage2b_mlofi_microprice.md`
**Symbol**: 2330 (TSMC equity), TXFD6 (Mini-TAIEX futures)

---

## Challenge 1: Non-Overlapping IC — RESOLVED

**Method**: Subsampled every 30s (non-overlapping windows), ~550 obs/day.

**Assessment**: Correct subsampling approach. The 35% IC drop (0.206 -> 0.134) is within the expected range for overlapping-to-non-overlapping conversion on autocorrelated returns. The NW t-stat of 19.51 with 17/17 positive days confirms this is not a statistical artifact from overlap inflation alone.

**One minor note**: The document reports ~550 obs/day for 30s intervals. A 4.5-hour TWSE session gives 540 non-overlapping 30s windows, so the counts are consistent. No issues with the subsampling mechanics.

**Verdict**: RESOLVED. The overlap inflation is quantified and the residual signal is still statistically significant.

---

## Challenge 2: Detrended IC — RESOLVED (with methodological caveat)

**Method**: 5-minute rolling mean removed from forward returns before computing Spearman IC.

**Result**: IC(30s) flips to -0.032 (t=-3.95). IC is negative at ALL horizons below 60s.

**Assessment**: The finding is damning for the alpha claim. The monotonic IC increase from 250ms to 60s in the raw data (Table in Challenge 2) is textbook trend contamination — genuine microstructure signals peak at a characteristic horizon and decay. The detrended analysis correctly exposes this.

**Methodological caveat on 5-min window**: The task asks whether 5 minutes is too aggressive. This is a legitimate concern:

- **Too short** (e.g., 1-2 min): would remove genuine microstructure mean-reversion signal along with trend. The resulting IC could be artificially negative.
- **Too long** (e.g., 15-30 min): would leave residual trend in, failing to separate microstructure from momentum.
- **5-minute window**: This is a reasonable middle ground for equity tick data. On 2330 with ~550 ticks/30s, a 5-min window spans ~5,500 ticks — enough to capture local trend without destroying sub-minute microstructure dynamics.

However, the fact that detrended IC is *strongly negative* (not merely zero) at short horizons (250ms: IC=-0.215) suggests the signal is genuinely contrarian on microstructure timescales. Even if the 5-min window is somewhat aggressive, a window of 10-15 minutes would likely still produce negative or near-zero detrended IC at 30s. The conclusion that the raw IC is trend-driven would hold.

**The real smoking gun** is not the detrended IC alone but the combination: (a) monotonically increasing IC with horizon, (b) detrended IC flipping negative, and (c) L1 dominance (Challenge 3). Any one of these could be debated; together they are conclusive.

**Verdict**: RESOLVED. The 5-min window is defensible. Even with a more generous window, the trend contamination conclusion would hold given the corroborating evidence.

---

## Challenge 3: L1 vs L2-L5 Decomposition — RESOLVED

**Result**: L1-only IC = +0.217, Full MLOFI IC = +0.206, Deep-only IC = +0.029.

**Assessment**: This is clean and unambiguous. L1 alone *outperforms* the full MLOFI, meaning L2-L5 levels introduce noise that dilutes the signal. The deep-only IC of +0.029 at 30s (NW t=7.53) is marginally significant but economically irrelevant — it is below any actionable threshold given transaction costs.

The finding demolishes the "multi-level order flow" thesis. The MLOFI name implies value from aggregating across book depth. In reality, on 2330, L1 imbalance is the entire signal.

**One additional point**: The document notes `lam=0.0` for L1-only, meaning pure L1 delta OFI with EMA smoothing. This is methodologically correct — it isolates the L1 contribution without re-weighting.

**Verdict**: RESOLVED. Deep levels add nothing. L1 dominance is proven.

---

## Challenge 4: OOS Incremental IC — RESOLVED (but reinterpreted)

**Result**: OOS incremental IC(30s) = +0.119, 14/16 days positive.

**Assessment**: The OOS stability is real — the regression coefficient transfers well day-to-day. However, the Researcher correctly notes that this does not validate microstructure alpha. The incremental IC measures whether MLOFI adds information beyond lagged mid-price returns. Since MLOFI is itself a trend-following signal (Challenge 2 finding), it can have stable OOS incremental IC simply by capturing momentum that the lagged return alone does not fully represent.

A trend-following signal is *expected* to show stable OOS incremental IC over lagged returns, because:
1. Trend persistence is a robust market property
2. EMA-smoothed OFI is a different representation of the same momentum
3. Day-to-day coefficient stability reflects trend persistence, not microstructure stability

The Researcher's interpretation is correct: PASS on the metric, but the metric does not prove what it was designed to prove (multi-level microstructure value).

**Verdict**: RESOLVED. The OOS IC is valid but correctly reinterpreted as trend-following evidence rather than microstructure validation.

---

## Challenge 5: Realized Spread Direction Fix — RESOLVED (with important qualification)

**Result**: Bug B3 fixed. Pro-cyclical direction now gives mean RS = +4,340 (0.434 NTD), positive all 17 days. Median = 0 on all days.

**Assessment**: The sign correction is correct — pro-cyclical is the right framing for a momentum/trend signal. The positive mean across all days is consistent.

**The median = 0 finding is critical and correctly flagged**. It means the majority of signal-triggered "fills" see zero mid-price movement at 30s. The positive mean is entirely driven by the right tail — large trending moves where the signal happens to be aligned. This is exactly what trend contamination looks like in realized spread terms:

- Most of the time: price doesn't move in 30s (median = 0)
- Occasionally: price trends and MLOFI is aligned (large positive RS)
- The mean is pulled positive by these occasional trend episodes

This distribution (heavy right tail, median at zero) is not tradeable. A strategy based on this signal would have zero edge on most entries and rely on occasional trend captures — which any simple momentum indicator would achieve equally well.

**Verdict**: RESOLVED. The bug is fixed and the data correctly interpreted. The tail-driven positive mean reinforces the trend contamination diagnosis.

---

## Overall Assessment

### All 5 Challenges: RESOLVED

| Challenge | Status | Key Finding |
|-----------|--------|-------------|
| 1. Non-overlapping IC | RESOLVED | IC drops 35% but remains significant (0.134) |
| 2. Detrended IC | RESOLVED | IC flips negative (-0.032 at 30s) — trend contamination confirmed |
| 3. L1 vs Deep | RESOLVED | L1 alone is better (0.217 vs 0.206); deep levels are noise |
| 4. OOS incremental IC | RESOLVED | Stable (+0.119) but reflects trend persistence, not microstructure |
| 5. Realized spread fix | RESOLVED | Positive mean, zero median — tail-driven, not tradeable |

### TERMINATE Recommendation: APPROVE

The TERMINATE recommendation is justified by converging evidence from multiple independent tests:

1. **Detrended IC is the decisive test**. A signal that loses all predictive power (and flips negative) when 5-min local trend is removed is, by definition, a trend proxy. This alone is sufficient grounds for termination.

2. **L1 dominance eliminates the novelty claim**. If the entire signal comes from L1 imbalance (already captured by `ofi_l1` in FeatureEngine), there is no new information from the multi-level construction.

3. **IC horizon profile is diagnostic**. The monotonic increase from 250ms to 60s is the signature of trend contamination. Genuine microstructure alpha peaks and decays. This pattern was noted but deserves emphasis — it should be added to the team's standard diagnostic checklist.

4. **TXFD6 is dead** (IC = -0.019, CV = 337%). No further investigation warranted.

### Remaining Methodology Issues: None Material

The analysis is thorough and the methodology is sound. The only debatable point is the 5-min detrending window, but as argued above, the conclusion is robust to reasonable window variations given the corroborating evidence.

### Recommendation for Future Research

The Researcher's pivot suggestion (L1 imbalance as momentum feature on 2330) is correct but redundant — `ofi_l1` already exists in FeatureEngine. No further work on MLOFI microprice correction is warranted.

**Overall: APPROVE TERMINATE**
