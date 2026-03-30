# Round 18 Stage 2a-bis: Night Session Diagnostic Results

**Date**: 2026-03-26
**Status**: Mixed -- VRB Night passes structural gates but has negative PnL. HMM dead everywhere.

---

## Data Inventory

| Session | Dates | 1-min bars | 5-min bars |
|---------|-------|------------|------------|
| Day (08:45-13:45) | 16 | ~3,600 | ~720 |
| Night (15:00-05:00) | 19 | ~10,800 | ~2,160 |
| Combined | 20 | ~14,400 | ~2,880 |

Night session adds approximately 3x the data vs day-only.

---

## VRB Results

### Day Session: KILLED (unchanged from prior run)
- 2 triggers in 16 sessions (0.12/session)
- All 3 kill gates FAIL

### Night Session: PASSES STRUCTURAL GATES (with caveats)
- **KG1 (trigger frequency)**: 28 triggers in 19 sessions = **1.47/session** -- PASS
- **KG2 (direction accuracy)**: EMA 38.1%, Reactive **55.6%** -- PASS (reactive)
- **KG3 (ToD distribution)**: Max bucket 50% -- PASS (spread across 2 time bands)

**However, the actual PnL is deeply negative:**
- Reactive direction PnL: **-18.0 pts/trade** (18 measurable trades)
- After cost (3.92 pts): **-21.9 pts/trade**
- Win rate 55.6% is misleading: wins are small (+2 to +48 pts), losses are large (-43 to -86 pts)

**Robustness concerns:**
- 9/28 triggers (32%) are from Jan 27 alone (first day, P20 unstable from expanding window warmup)
- 6/28 triggers are from Feb 10 with zero 5-min returns (illiquid late-night period)
- Excluding first 2 dates: avg drops to 0.82/session (below kill gate)
- EMA accuracy on late events: 66.7% (but N=9 only)

### Combined: PASSES STRUCTURAL GATES (same caveats)
- 40 triggers in 20 sessions = 2.0/session
- Reactive accuracy 64.3% (N=28)
- Max ToD bucket 25%
- Heavy concentration on Jan 27-28 (early unstable period)

### VRB Verdict
The night session VRB passes the kill gates, but the **economic edge is negative**. The signal detects vol expansion events correctly, but the directional prediction is not profitable because:
1. Large losses on wrong-direction trades dominate small wins
2. Early-date triggers inflate the count (P20 warmup artifact)
3. Illiquid late-night triggers (Feb 10) produce zero-return events

**Recommendation: VRB is structurally interesting on night session but NOT ready for prototype.** The reactive direction signal would need additional filtering (minimum move size, liquidity check, late-night exclusion) to potentially become viable. Risk: N=28 is far too small for any confidence.

---

## HMM Results

### Day Session: KILLED (unchanged)
- Separation ratio: 0.024 (need >= 1.0)
- One state degenerate (sigma = 0.0)

### Night Session: KILLED
- State 0: mu=0.58, sigma=46.96 (broad)
- State 1: mu=1.09, sigma=11.92 (narrow)
- **Separation ratio: 0.006** (even worse than day-only)
- Mu difference: 0.51 pts vs 2*max_sigma=93.9 pts
- OOS: momentum PnL -0.56 pts/trade, reversion PnL -4.15 pts/trade
- Both trading directions are unprofitable

### Combined: KILLED
- Separation ratio: 0.010
- OOS momentum: +2.24 pts/trade (barely positive, N=844)
- OOS reversion: -0.06 pts/trade (flat)
- After cost (3.92 pts): momentum goes deeply negative
- **The HMM simply cannot distinguish meaningful states in TMFD6 returns at any session**

### HMM Verdict
The 2-state HMM is **structurally inappropriate** for TMFD6 because:
1. 5-min returns are unimodal (single Gaussian, no regime structure)
2. The two states separate by volatility level (sigma), not by drift (mu) -- this is a vol regime, not a momentum/reversion regime
3. Night session state 1 (sigma=11.9) vs state 0 (sigma=47.0) is a high-vol vs low-vol split, but both have near-identical means
4. The HMM is reinventing what RV already measures, adding no incremental information

---

## Critical Observations

### Night vs Day Structural Differences
The night session (15:00-05:00) is fundamentally different from day:
- **3x more data**: 19 dates vs 16, ~680 bars/session vs ~255
- **More vol compression/expansion**: 1.47 triggers/session vs 0.12 (12x more)
- **But lower liquidity**: Feb 10 triggers had 0.0 five-min returns (no real price movement)
- **Longer duration**: 14h session allows more room for compression cycles

### Data Quality Issue
The expanding P20 percentile is unstable on the first 2-3 dates because it's calibrated on very few prior observations. This inflates trigger counts early in the sample. A fixed 20-day lookback would eliminate most triggers given the data gaps.

---

## Summary Table

| Diagnostic | Day | Night | Combined |
|------------|-----|-------|----------|
| **VRB KG1** (triggers) | FAIL (0.12) | PASS (1.47) | PASS (2.00) |
| **VRB KG2** (direction) | FAIL | PASS (55.6%) | PASS (64.3%) |
| **VRB KG3** (ToD) | FAIL | PASS (50%) | PASS (25%) |
| **VRB PnL** | N/A | **-18.0 pts** | N/A |
| **HMM KG** (separation) | FAIL (0.024) | FAIL (0.006) | FAIL (0.010) |
| **HMM PnL** | -0.92 | -0.56 / -4.15 | +2.24 / -0.06 |

## Files
- Script: `research/experiments/validations/vrb_diagnostic/vrb_hmm_night_diagnostic.py`
- Results: `research/experiments/validations/vrb_diagnostic/night_diagnostic_results.json`
