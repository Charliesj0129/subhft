# Round 17 Stage 2: Prototype Results

Date: 2026-03-26

## Candidate 2: Multi-Timescale Trend Reversion (MSTR)

**Location**: `research/alphas/multiscale_trend_reversion/`
**Paper**: arXiv:2501.16772 (Safari & Schmidhuber 2025)

### Signal Design

Compute standardized trend strength phi at horizons (2, 4, 8, 16, 32, 64 min)
using EMA-based t-statistic. Cubic model: E[R] = a + b*phi + c*phi^3.
Paper claims universal mean-reversion at sub-hour scales.

### IC Results (20 days TMFD6 L1, per-day Spearman rank IC pooled)

| phi\fwd | fwd_1min | fwd_5min | fwd_10min | fwd_30min |
|---------|----------|----------|-----------|-----------|
| phi_2min | +0.048 (t=4.4) | +0.049 (t=4.8) | +0.037 (t=3.1) | +0.003 (t=0.1) |
| phi_4min | +0.044 (t=6.1) | +0.047 (t=6.0) | +0.037 (t=3.6) | +0.019 (t=1.0) |
| phi_8min | +0.041 (t=9.0) | +0.045 (t=4.4) | +0.039 (t=3.0) | +0.037 (t=2.0) |
| phi_16min| +0.037 (t=6.8) | +0.040 (t=3.4) | +0.037 (t=2.5) | +0.039 (t=1.6) |
| phi_32min| +0.022 (t=6.4) | +0.018 (t=2.1) | +0.013 (t=0.9) | +0.012 (t=0.5) |
| phi_64min| +0.003 (t=0.2) | -0.011 (t=-0.5)| -0.018 (t=-0.6)| -0.035 (t=-0.9)|

### Cubic Fit

ALL cubic c coefficients NOT statistically significant (|t| < 2.0).
The paper's key tradeable mechanism (reversion at phi_c) is absent on TMFD6.

### Verdict: FAIL as contrarian alpha

TMFD6 shows **momentum** (positive IC) at sub-hour scales, NOT mean-reversion.
The Schmidhuber "universal" reversion does not apply to this instrument.

**Unexpected finding**: phi_8min is a statistically significant momentum indicator
(IC = +0.041, t = 9.0, 90% of days positive). Useful as a feature/filter.

---

## Candidate 3: Regime-Adaptive OFI (RA-OFI)

**Location**: `research/alphas/regime_adaptive_ofi/`
**Papers**: arXiv:2505.17388 (Hu & Zhang), arXiv:2307.02375 (Tsaknaki et al.)

### Signal Design

Classify ticks into regimes (quiet/normal/volatile) via spread EMA.
Adapt OFI EMA window per regime (60/20/8 ticks). Block signals in volatile regime.
Apply quasi-Sharpe filter: only trade when expected edge > cost.

### Regime Distribution (7.7M ticks TMFD6)

| Regime   | Ticks     | Pct   |
|----------|-----------|-------|
| Quiet    | 4,867,232 | 62.8% |
| Normal   | 1,782,142 | 23.0% |
| Volatile | 1,097,940 | 14.2% |

### IC Results (7.7M ticks, Spearman rank IC)

**Unconditional OFI (EMA-20, all regimes):**

| fwd_1min | fwd_5min | fwd_10min | fwd_30min |
|----------|----------|-----------|-----------|
| +0.061   | +0.054   | +0.043    | +0.025    |

**Per-regime IC (standard EMA-20):**

| Regime   | fwd_1min | fwd_5min | fwd_10min | fwd_30min |
|----------|----------|----------|-----------|-----------|
| Quiet    | +0.046   | +0.034   | +0.024    | +0.013    |
| Normal   | +0.109   | +0.126   | +0.100    | +0.055    |
| Volatile | +0.116   | +0.122   | +0.108    | +0.073    |

**Regime-adapted IC (adaptive EMA window):**

| Scope    | fwd_1min | fwd_5min | fwd_10min | fwd_30min |
|----------|----------|----------|-----------|-----------|
| All      | +0.061   | +0.056   | +0.046    | +0.027    |
| Quiet    | +0.050   | +0.040   | +0.032    | +0.016    |
| Normal   | +0.109   | +0.126   | +0.100    | +0.055    |
| Volatile | +0.109   | +0.110   | +0.097    | +0.067    |

**Session IC (regime-adapted):**

| Session  | fwd_1min | fwd_5min | fwd_10min | fwd_30min |
|----------|----------|----------|-----------|-----------|
| Opening  | +0.069   | +0.052   | +0.044    | +0.031    |
| Rest     | +0.079   | +0.074   | +0.062    | +0.036    |

### Key Comparison

| Metric | fwd_1min | fwd_5min | fwd_10min | fwd_30min |
|--------|----------|----------|-----------|-----------|
| Unconditional | +0.061 | +0.054 | +0.043 | +0.025 |
| Regime-adapted | +0.061 (+1%) | +0.056 (+4%) | +0.046 (+9%) | +0.027 (+5%) |
| Quiet-only | +0.050 (-17%) | +0.040 (-26%) | +0.032 (-24%) | +0.016 (-36%) |

### Verdict: FAIL -- regime conditioning does NOT materially improve IC

**Critical finding**: Regime conditioning improves IC by only 1-9%, far below
the 50%+ needed to overcome the cost barrier. Worse, the "quiet" regime
(62.8% of time) has LOWER IC than the unconditional average.

**Counter-intuitive result**: OFI IC is HIGHEST in volatile regimes (+0.116)
and LOWEST in quiet regimes (+0.046). This is the opposite of the hypothesis
that quiet = better signal. Wide spreads correlate with larger price moves,
which mechanically increases IC, but those are exactly the moments when
adverse selection is worst and costs are highest.

**The R16 finding holds**: L1 OFI is structurally insufficient to overcome
the 4-pt cost barrier on TMFD6, regardless of regime conditioning.

---

## Structural Insights from Both Prototypes

1. **TMFD6 is a momentum instrument** at sub-hour scales (phi IC positive, OFI IC positive)
2. **Volatility amplifies signal but also cost**: IC is highest when spreads are widest
3. **Quiet regimes are the WORST for OFI**: contradicts "favorable regime" hypothesis
4. **Opening vs rest-of-day**: rest-of-day has slightly better OFI IC (+0.079 vs +0.069)
5. **phi_8min (IC=0.041, t=9.0)** is the strongest non-OFI signal found -- pure momentum

## Recommendation for Round 17

Both Candidates 2 and 3 FAIL as standalone alphas. The most promising path forward
is **Candidate 1 (TX-TMF Lead-Lag)** which was not prototyped in this stage but
exploits cross-asset information not available in single-instrument analysis.

Alternatively, the momentum finding (phi_8min) could be combined with CBS as a
trend-following confirmation filter.
