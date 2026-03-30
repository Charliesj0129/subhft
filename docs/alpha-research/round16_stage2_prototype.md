# Round 16 Stage 2: Prototype Results

## Candidate C: Fill Probability-Conditioned Entry Filter

### Paper Analysis

**Albers et al. (2502.18625)** documents the fill probability vs post-fill returns trade-off using live Binance BTC data. Key findings relevant to our adaptation:

1. **Negative correlation** between fill probability and post-fill returns is a fundamental LOB mechanic — not market-specific
2. **Reversals** (contrarian fills with positive returns) occur when imbalance falsely predicts price direction. A logistic regression on LOB features achieves economically significant reversal prediction
3. **Key features**: `ret_autocov_5s` (negative autocov = oscillation = reversal likely), `ret_sum_100ms` (sudden drop = reversal), `depth_imbalance`, `OFI`
4. **AUC ~0.55-0.60** is sufficient for economic value — even false positive predictions yield better-than-random fills

**Lokin & Yu (2403.02572)** provides analytical fill probability expressions via first-passage times of state-dependent birth-death processes. Their queue-position-dependent formulas require data we lack (queue position), but their insight that spread state affects fill probability maps to our available features.

### Implementation

- **Location**: `research/alphas/fill_prob_filter/`
- **Model**: Logistic regression, 7 LOB-state features, no queue position dependency
- **Features**: spread_scaled, depth_imbalance (sign-adjusted), ofi_l1_ema8 (sign-adjusted), L1 qty ratio, spread/ema ratio, depth_imbalance_ema, spread*imbalance interaction
- **Training**: Gradient descent with L2 regularization (lambda=0.1), 200 iterations

### Backtest Results (TXFD6, 13 days, 18,140 simulated fills)

| Metric | Value |
|--------|-------|
| Total fills | 18,140 (IS: 10,884, OOS: 7,256) |
| AUC IS | **0.7321** |
| AUC OOS | **0.5560** (> 0.55 target) |
| Adverse fill rate (baseline) | 34.8% |
| Mean post-fill return (baseline) | **+1.095 bps** |
| Filter pass rate | 99.9% |
| PnL improvement from filtering | ~0 bps |

### Key Findings

1. **OOS AUC = 0.556 PASSES the 0.55 threshold**, but just barely. The model has marginal predictive power outside of training data.

2. **Adverse fill rate in wide-spread regime is only 34.8%** — significantly lower than the 49-59% cited in OpMM documentation for all wide-spread events. This suggests the OpMM's spread gate already filters out the worst adverse selection periods.

3. **Mean post-fill return is POSITIVE (+1.095 bps)** in the wide-spread regime. This means wide-spread entries are, on average, profitable even before the filter. The filter has little room to improve.

4. **The filter barely rejects any entries** (pass rate 99.9%). Because the base adverse rate is only 34.8% and the model's predicted P(adverse) rarely exceeds 0.5, almost all entries pass even with aggressive thresholds.

5. **Spread is the dominant feature** (weight = -0.35, negative = wider spread reduces adverse probability). This confirms the OpMM's spread gate is already performing the primary filtering function.

6. **Albers' contrarian finding partially transfers**: the negative spread coefficient means the model learns that wider spreads reduce adverse selection risk (consistent with Albers' finding that fill quality improves in wider-spread regimes).

### Verdict: MARGINAL — Filter adds negligible value on top of OpMM's existing spread gate

The OpMM's spread threshold (2.5 bps) already captures the essence of the fill quality filter. Adding a logistic regression on top provides at most 0.556 AUC (barely above random) and cannot meaningfully improve the already-positive +1.095 bps mean return. **Not recommended for production integration** unless combined with a more sophisticated signal (e.g., ret_autocov from Albers, which we lack in current FeatureEngine).

### Possible Future Enhancement
Adding `ret_autocov_5s` to FeatureEngine v2 (already included as feature [17]) could improve AUC significantly, as Albers identifies this as the most important reversal predictor.

---

## Candidate A: Latency-Aware Inventory Skew Optimization

### Paper Analysis

**Barzykin (2603.07752)** develops an OTC FX market-making model with:
- Adiabatic quadratic approximation for tractable controls
- Riccati ODE for the inventory penalty coefficient A(t)
- Trade rejection and reputation feedback mechanics

**CLOB-compatible subset** (after stripping OTC-specific mechanics):

After removing RFQ/rejection/reputation variables, what survives is the standard **Avellaneda-Stoikov / Gueant et al. (2013)** result:

1. Quadratic value function: `V(t,q) = -A(t)*q^2 - C(t)`
2. Riccati ODE: `A'(t) + gamma*sigma^2/2 = 4*A(t)^2 * Sigma`
3. Stationary solution: `A = sqrt(gamma*sigma^2 / (8*g*Sigma))`
4. Optimal quote: `delta*(q) = 1/kappa + A*(z +/- 2*q)`
5. Skew is linear in q: `skew(q) = 2*A*q`

**Critical insight**: The Riccati-optimal skew IS linear in inventory (same functional form as SimpleMarketMaker). The difference is only in the **coefficient magnitude**, which depends on calibrated parameters (gamma, sigma, kappa, lambda_0).

**Chavez-Casillas (2405.11444)** adds state-dependent fill rates via recent MO history (lag-phi Bernoulli indicators). Their adaptive fill model requires fill probability estimation infrastructure we currently lack, so only the base AS framework was implemented.

### Implementation

- **Location**: `research/alphas/inventory_skew_opt/`
- **Riccati solver**: RK4 backward integration with clamping
- **Calibration**: Automated from TXFD6 L1 data (sigma from tick returns, kappa from spread, lambda from tick rate)
- **Comparison**: Linear vs Riccati skew at inventory levels 0-10

### Comparison Results (TXFD6 calibrated)

| Parameter | Calibrated Value |
|-----------|-----------------|
| sigma | 5.25 pts/sqrt(s) |
| gamma | 0.47 |
| kappa | 0.006 /pt |
| lambda_0 | 2.51 ticks/s |

| Inventory | Linear Skew (pts) | Riccati Skew (pts) | Diff (bps) |
|-----------|-------------------|-------------------|------------|
| 1 | 0.50 | 34.93 | 10.6 |
| 3 | 1.50 | 104.80 | 31.9 |
| 5 | 2.50 | 174.67 | 53.2 |
| 10 | 5.00 | 349.33 | 106.4 |

### Key Findings

1. **The Riccati-optimal skew is ~70x larger than the current linear skew.** This indicates the current SimpleMarketMaker's INVENTORY_SKEW_DIVISOR=5 produces dramatically insufficient inventory risk penalization per the AS model.

2. **HOWEVER, this comparison is misleading.** The AS model's kappa is calibrated from the spread distribution, giving `1/kappa = 175 pts` half-spread — absurdly wide for a 5-point-spread instrument. TXFD6 is a tick-constrained market where the minimum spread is 1 point (1 tick). The AS exponential-intensity model assumes continuous spreads, which breaks down when spread is only 1-5 ticks.

3. **The Riccati solution converges quickly** to its stationary value. A(t) reaches 95% of A_stationary within seconds of the horizon start, confirming that near-stationary behavior dominates for multi-hour horizons.

4. **Adiabatic approximation is valid**: tau/T_tick = 36ms/125ms = 0.29 < 1.

5. **The optimal skew is LINEAR (same form as current)** — the Riccati solution for the standard AS model gives skew = 2*A*q, which is linear in inventory. There is no nonlinear improvement from the Riccati approach. The Challenger concern was correct: at small inventory, the Riccati solution is near-linear.

6. **Sensitivity to gamma**: Even at gamma*0.1 (very low risk aversion), the Riccati skew is still ~10x larger than the current linear skew. The gap is structural, not a parameter choice issue.

### Interpretation

The 70x gap between calibrated Riccati and current linear skew reflects a **model mismatch**, not a genuine optimization opportunity:

- **Current SimpleMarketMaker** sets skew as a fraction of the spread (spread * 50% / 5 = 10% of spread per unit). This is a practical, spread-relative heuristic.
- **AS/Riccati model** assumes continuous Poisson arrivals with exponential intensity decay, calibrating gamma to produce "reasonable" inventory penalty. But the exponential-intensity model's kappa parameter maps to half-spreads of ~175 points, whereas TXFD6's actual half-spread is ~2.5 points.

The AS framework was designed for FX and US equity markets where spread is quasi-continuous. For tick-constrained markets like TXFD6 (1 point = 10 NTD, spread = 1-5 ticks), the model's exponential intensity assumption is a poor fit.

### Verdict: REJECTED — AS/Riccati model is structurally incompatible with tick-constrained TXFD6

The optimal skew formula is the same functional form (linear in q) as the current implementation. The coefficient calibration via the AS model is unreliable for tick-constrained markets. The current heuristic (spread-relative with divisor=5) is actually a more appropriate parameterization for TXFD6 than the AS exponential-intensity model.

**However**, the finding that the current skew coefficient may be too small warrants investigation. A parameter sweep of INVENTORY_SKEW_DIVISOR (e.g., 2, 3, 5, 8, 10) on historical PnL could be more productive than the theoretical approach.

---

## Summary Table

| Candidate | OOS AUC / Key Metric | Verdict | Next Step |
|-----------|---------------------|---------|-----------|
| C: Fill Prob Filter | AUC=0.556, +0 bps improvement | MARGINAL | Consider adding ret_autocov_5s feature |
| A: Inventory Skew Opt | Linear-same-form, model mismatch | REJECTED | Parameter sweep of existing divisor instead |

## Files Created

```
research/alphas/fill_prob_filter/
  __init__.py
  manifest.yaml
  impl.py        -- AdverseFillModel, FillProbabilityFilter, feature extraction
  backtest.py    -- Full backtest harness on TXFD6 L1 data

research/alphas/inventory_skew_opt/
  __init__.py
  manifest.yaml
  impl.py        -- RiccatiSolution solver, MarketParams calibration, RiccatiSkewCalculator
  comparison.py  -- Full linear vs Riccati comparison with sensitivity analysis
```
