# Round 21 Stage 1: Literature Survey
## VPIN Gamma Scaling (4.2.2) + Conditional Wide-Spread Capture (4.3)

**Date**: 2026-03-27
**Researcher**: Claude (Opus 4.6)
**Status**: Stage 1 Complete -- Ready for Challenger Review

---

## 1. Executive Summary

This survey evaluates two enhancement directions for existing TMFD6 market-making strategies:

1. **VPIN-Driven Dynamic Risk Aversion (4.2.2)**: Using the existing VPIN regime signal to dynamically scale the risk aversion parameter gamma in the Avellaneda-Stoikov framework.
2. **Conditional Wide-Spread Capture (4.3)**: Conditioning the OpportunisticMM spread threshold on market state (VPIN regime, time-of-day, volatility) to improve profitability.

**Bottom line**: Direction 4.3 (Conditional Spread Capture) is recommended for prototyping first. It has a clearer implementation path, lower risk, and directly addresses a known profitable regime (wide-spread periods). Direction 4.2.2 (VPIN Gamma Scaling) is theoretically elegant but faces a fundamental obstacle: the R12/R16 results show that TMFD6 median spread (3 pts) is below breakeven (4 pts), meaning gamma scaling cannot rescue an unviable base case.

---

## 2. Paper Summaries

### 2.1 VPIN + MM Risk Aversion Literature

#### P1. Avellaneda & Stoikov (2008) -- "High-frequency trading in a limit order book"
- **Key finding**: Seminal framework for optimal MM quoting. The market maker maximizes CARA utility with risk aversion gamma. Optimal quotes are:
  - Reservation price: r(s, q, t) = s - q * gamma * sigma^2 * (T - t)
  - Optimal spread: delta_bid + delta_ask = gamma * sigma^2 * (T-t) + (2/k) * ln(1 + gamma/k)
  - Where q = inventory, sigma = volatility, k = order arrival decay, T = horizon
- **Relevance**: **5/5** -- This IS the framework we would modify. The gamma parameter directly controls how aggressively the MM quotes. Higher gamma = wider quotes = less inventory risk but fewer fills.
- **TMFD6 applicability**: The closed-form approximation assumes continuous quoting and sufficient fill rate. On TMFD6 with 36ms RTT and median spread 3 pts, the fill rate is already extremely low.

#### P2. Gueant, Lehalle & Fernandez-Tapia (2011/2012) -- "Dealing with the Inventory Risk" [arXiv: 1105.3115]
- **Key finding**: Extends Avellaneda-Stoikov with inventory constraints (max position Q_max). Derives closed-form approximations via spectral decomposition. The optimal quotes under inventory constraints are more conservative near position limits.
- **Relevance**: **4/5** -- Provides the practical formulas for implementing gamma-dependent quoting with position limits. Their asymptotic approximation:
  - delta_bid* = (1/k) + (gamma * sigma^2 * (T-t)) / 2 * (2q - 1) for inventory skew
  - delta_ask* = (1/k) + (gamma * sigma^2 * (T-t)) / 2 * (1 - 2q)
- **TMFD6 applicability**: The 1/k baseline spread term must exceed the tick size. On TMFD6 with tick = 1 pt, this is satisfied. But the gamma-dependent inventory adjustment is tiny relative to the fixed spread cost.

#### P3. Fodra & Labadie (2012) -- "HF market-making with inventory constraints and directional bets" [arXiv: 1206.4810]
- **Key finding**: Introduces an explicit inventory-risk-aversion parameter eta in a quadratic penalty: phi(s, q, x) = x + qs - eta*q^2. This eta parameter:
  - Controls PnL variance, skewness, kurtosis, and VaR
  - Can increase Sharpe ratio by >2x when tuned (at cost of 5% avg PnL)
  - Or increase avg PnL by >15% (at cost of much higher risk)
  - Numerically solved for Ornstein-Uhlenbeck (mean-reverting) mid-price
- **Relevance**: **5/5** -- Directly demonstrates that a single risk-aversion parameter controls the entire risk-reward profile. This is exactly the parameter we would make VPIN-dependent.
- **Critical insight**: Their eta is analogous to our proposed gamma(VPIN). The paper shows the parameter sensitivity is smooth and monotonic -- small changes in eta produce proportional changes in risk metrics.

#### P4. Easley, Lopez de Prado & O'Hara (2012) -- "Flow Toxicity and Liquidity in a High-frequency World" (VPIN original, non-arXiv)
- **Key finding**: VPIN (Volume-Synchronized Probability of Informed Trading) provides a real-time estimate of order flow toxicity. It is computed from volume bars using bulk volume classification. Higher VPIN = more informed trading = more adverse selection for market makers.
- **Relevance**: **5/5** -- This is the signal we already have implemented. The key question is whether VPIN regime changes are timely enough to be actionable for gamma adjustment.
- **Known limitation (from R12/R19)**: VPIN is essentially a volume intensity proxy. As an MM overlay on TXFD6, it produced DD -30.6%. The signal detects regime AFTER the fact, not before.

#### P5. Fang et al. (2019) -- "Design of High-Frequency Trading Algorithm Based on Machine Learning" [arXiv: 1912.10343]
- **Key finding**: Combines VPIN with GARCH and SVM for market-making on CSI300 futures. Uses VPIN to pre-judge market liquidity before placing orders.
- **Relevance**: **2/5** -- Confirms the idea of using VPIN as an input to MM decisions, but the ML framework adds complexity without clear benefit over a simple threshold rule. The CSI300 context is far more liquid than TMFD6.

#### P6. Gueant (2016) -- "Optimal Market Making" [arXiv: 1605.01862]
- **Key finding**: Comprehensive generalization of Avellaneda-Stoikov framework. Provides the canonical closed-form approximation:
  - Optimal half-spread: delta* = (1/gamma) * ln(1 + gamma/k) + q * gamma * sigma^2 * (T - t) / 2
  - This simplifies to: delta* ~ 1/k + gamma * sigma^2 * tau / 2 for small gamma
- **Relevance**: **4/5** -- Confirms that gamma appears linearly in the spread formula. Doubling gamma roughly doubles the inventory-dependent spread component. This is the formula we would parameterize.

#### P7. Back et al. (2020) -- "Optimal Transport and Risk Aversion in Kyle's Model" [arXiv: 2006.09518]
- **Key finding**: With risk-averse market makers, liquidity is lower, assets exhibit short-term reversals, and risk premia depend on MM inventories. Shows that risk aversion creates mean-reverting inventory dynamics.
- **Relevance**: **3/5** -- Theoretical support for the idea that dynamic risk aversion changes market behavior. Not directly implementable but confirms the economic logic.

#### P8. Barzykin, Bergault & Gueant (2025) -- "Optimal Quoting under Adverse Selection and Price Reading" [arXiv: 2508.20225]
- **Key finding**: Market makers adjust quotes with awareness of informational risk (adverse selection from informed traders + "price reading" where competitors extract signal from your quotes). Provides a tractable framework for adjusting quotes based on estimated toxicity.
- **Relevance**: **4/5** -- Directly addresses the problem of adjusting quoting based on estimated flow toxicity. Their framework is more sophisticated than simple gamma scaling but validates the core intuition.

### 2.2 Conditional Spread Capture Literature

#### P9. Sarkissian (2016) -- "Spread, volatility, and volume relationship in financial markets" [arXiv: 1606.07381]
- **Key finding**: Spread is a function of volatility, volume, and time horizon: S ~ sigma * sqrt(V) * f(T). Derives the operating spread optimization problem to maximize MM profit. Key result: there exists an optimal spread that balances fill rate against edge per trade.
- **Relevance**: **4/5** -- Provides the theoretical basis for why the spread threshold should be a function of volatility (not a constant). Higher volatility = wider natural spread = more profitable MM opportunity.

#### P10. Wyart, Bouchaud et al. (2006) -- "Relation between Bid-Ask Spread, Impact and Volatility" [arXiv: physics/0603084]
- **Key finding**: Linear relationship between bid-ask spread and instantaneous market order impact. Strong correlation (R^2 > 0.9) between spread and volatility-per-trade. Main determinant of spread is adverse selection.
- **Relevance**: **5/5** -- Establishes that spread IS a function of volatility and adverse selection intensity. This means:
  - Wide spread periods = high volatility/toxicity periods
  - The OpMM's spread threshold implicitly selects for these periods
  - Conditioning on volatility is equivalent to being more precise about WHEN wide spreads are profitable vs dangerous

#### P11. Ruan, Bacry & Muzy (2023) -- "The self-exciting nature of bid-ask spread dynamics" [arXiv: 2303.02038]
- **Key finding**: Spread dynamics are well-modeled by a State-dependent Spread Hawkes model (SDSH). Spread jumps cluster (self-excite) and the current spread level affects future spread transition intensities. Successfully forecasts spread at short horizons.
- **Relevance**: **3/5** -- Confirms that spread regime is predictable at short horizons. If we can forecast when spread will be wide for the next N seconds, we can pre-position quotes.

#### P12. Wang, Ventre & Polukarov (2025) -- "Robust Market Making: To Quote, or not To Quote" [arXiv: 2508.16588]
- **Key finding**: RL-trained MM agents that can choose NOT to quote (or quote single-sided) outperform continuously quoting agents. The "to quote or not" decision is effectively a conditional activation problem -- exactly what OpMM already does with its spread threshold.
- **Relevance**: **4/5** -- Validates the OpMM architecture (selective quoting > continuous quoting). Their finding that one-sided quoting can be optimal suggests our reversal filter direction is correct.

#### P13. Wang, Ventre & Polukarov (2025) -- "ARL-Based Multi-Action Market Making with Hawkes + Variable Volatility" [arXiv: 2508.16589]
- **Key finding**: MM trained in low-volatility environments adapts well to high-volatility. 4-action MM (quote both / quote bid only / quote ask only / don't quote) provides two-sided quotes >=92% of the time.
- **Relevance**: **3/5** -- Confirms volatility regime adaptation is valuable. Their fixed volatility levels (2 vs 200) are crude; our approach of conditioning on VPIN/volatility is more granular.

#### P14. Chavez-Casillas et al. (2024) -- "Adaptive Optimal Market Making Strategies" [arXiv: 2405.11444]
- **Key finding**: Discrete-time MM approach with adaptive strategies that react to order behavior online. Adaptive strategies significantly outperform fixed-distance quoting.
- **Relevance**: **3/5** -- Supports the principle that adapting to market conditions beats fixed parameters. Their adaptation is to order arrival rates; ours is to spread/toxicity regime.

#### P15. Gao & Wang (2018) -- "Optimal Market Making in the Presence of Latency" [arXiv: 1806.05849]
- **Key finding**: Latency is an additional source of risk that negatively impacts MM performance. A market maker can earn positive expected profit only if sufficient uninformed orders hit limit orders relative to the rate of price jumps. Profitability requires a sufficiently long trading horizon.
- **Relevance**: **4/5** -- Directly applicable to TMFD6 with 36ms latency. Their profitability condition (enough uninformed flow vs price jumps) maps to our spread threshold: we only quote when the spread is wide enough that even with latency, the edge exceeds costs.

---

## 3. Candidate Implementations

### Candidate A: VPIN-Conditioned Gamma Scaling (Direction 4.2.2)

**Concept**: Dynamically adjust the risk aversion parameter gamma in the AS-framework based on VPIN regime, making the MM more conservative during toxic flow and more aggressive during calm periods.

**Formula**:
```
gamma_effective = gamma_base * (1 + alpha * vpin_score)

where:
  gamma_base  = baseline risk aversion (calibrate: 0.001 - 0.1)
  alpha       = scaling sensitivity (calibrate: 0.5 - 5.0)
  vpin_score  = normalized VPIN (0.0 = minimum, 1.0 = maximum toxicity)

Reservation price (scaled int):
  r = mid_price_x2 / 2 - q * gamma_effective * sigma_sq_scaled * tau

Optimal half-spread (scaled int):
  half_spread = (1 / k) + gamma_effective * sigma_sq_scaled * tau / 2

Quoting rule:
  - If regime == TOXIC:  gamma_eff -> gamma_base * 3.0  (widen quotes, reduce fills)
  - If regime == ELEVATED: gamma_eff -> gamma_base * 1.5
  - If regime == LOW:    gamma_eff -> gamma_base * 0.5   (tighten quotes, seek fills)
```

**Parameters**:
| Parameter | Range | Default | Source |
|-----------|-------|---------|--------|
| gamma_base | 0.001 - 0.1 | 0.01 | Calibrate from TMFD6 volatility |
| alpha | 0.5 - 5.0 | 2.0 | Scale factor for VPIN sensitivity |
| sigma (30s RV) | Dynamic | From FE v2 | ret_autocov_5s proxy |
| k (order intensity) | 50 - 500 | Calibrate | From tick arrival rate |
| tau (horizon) | 30s - 300s | 60s | Holding horizon |
| VPIN regime thresholds | P75/P95 | Auto-calibrated | Existing VpinRegimeSwitch |

**Integration with existing code**:
- VPIN computation: Reuse `VolumeBarBuilder`, `BulkVolumeClassifier`, `VPINCalculator`, `RegimeDetector` from `vpin_regime_switch.py`
- Signal flow: `VpinRegimeSwitchStrategy` emits signal (+1/0/-1) -> OpMM reads signal via strategy coordinator -> OpMM adjusts internal gamma
- Hot-path: gamma_effective is a single int multiplication (no float in accounting)
- Feature dependency: sigma from `ret_autocov_5s_x1e6` (FE v2 index 17)

**Kill criteria**:
1. **Fundamental blocker**: If median spread remains < 4 pts (March pattern), gamma scaling cannot help -- there is no positive-edge regime to optimize. KILL if March spread distribution persists into April.
2. **VPIN lag**: If VPIN regime transitions lag price moves by > 30 seconds on average, the gamma adjustment arrives too late. KILL if median regime transition lag > 30s.
3. **Overfitting**: If optimal gamma_base varies by >3x across months, the parameter is not stable. KILL if walk-forward shows non-stationary optimal gamma.
4. **Marginal improvement**: If gamma scaling adds < 0.5 bps/trade over static gamma on the subset of wide-spread periods. KILL if improvement < 0.5 bps.

**Expected edge vs baseline**:
- **Optimistic**: +1-2 bps/trade by avoiding fills during toxic regimes and capturing more during calm periods.
- **Realistic**: +0.3-0.5 bps/trade. VPIN is a lagging signal (R12/R19 evidence). Most of the benefit comes from the "don't trade during TOXIC" rule, which is already partially captured by the spread gate.
- **Key risk**: This is solving a second-order problem (optimize gamma within already-filtered trades) when the first-order problem (spread < breakeven most of the time) remains unsolved.

### Candidate B: State-Conditional Spread Threshold (Direction 4.3)

**Concept**: Replace the fixed `spread_threshold_pts = 5` with a dynamic threshold conditioned on market state: VPIN regime, time-of-day, and realized volatility. Lower the threshold during favorable conditions (calm market, low toxicity) and raise it during unfavorable conditions (high toxicity, session transitions).

**Algorithm pseudocode**:
```
def compute_dynamic_threshold(vpin_regime, tod_minutes, rv_30s, base_threshold=5):
    """
    Returns dynamic spread threshold in points (integer).

    Rules:
    1. Base threshold = 5 pts (1 pt above breakeven)
    2. VPIN regime adjustment:
       - LOW:      -1 pt (threshold = 4 pts, breakeven trading)
       - ELEVATED:  0 pt (threshold = 5 pts, standard)
       - TOXIC:    +2 pts (threshold = 7 pts, only very wide spreads)
    3. Time-of-day adjustment:
       - Opening (08:45-09:15): +1 pt (volatile, adverse selection)
       - Close (13:15-13:45): +1 pt (session-end risk)
       - Midday (10:00-12:00): -0 pt (calm period)
    4. Volatility adjustment:
       - If RV_30s > 2x median: +1 pt (volatile, wider threshold)
       - If RV_30s < 0.5x median: -1 pt (calm, tighter threshold)
    5. Clamp to [4, 10] range (never below breakeven, never miss all trades)
    """
    threshold = base_threshold

    # VPIN adjustment
    if vpin_regime == TOXIC:
        threshold += 2
    elif vpin_regime == LOW:
        threshold -= 1

    # ToD adjustment
    if 525 <= tod_minutes <= 555:   # 08:45-09:15
        threshold += 1
    elif 795 <= tod_minutes <= 825:  # 13:15-13:45
        threshold += 1

    # Volatility adjustment (rv_30s is scaled x1e6)
    if rv_30s > rv_median_2x:
        threshold += 1
    elif rv_30s < rv_median_half:
        threshold -= 1

    return max(4, min(threshold, 10))
```

**Parameters**:
| Parameter | Range | Default | Source |
|-----------|-------|---------|--------|
| base_threshold | 4-6 pts | 5 | Current OpMM default |
| vpin_toxic_adder | 1-3 pts | 2 | From VPIN regime signal |
| vpin_low_subtractor | 0-1 pts | 1 | Conservative: never below breakeven |
| tod_open_adder | 0-2 pts | 1 | From R14 CBS ToD analysis |
| tod_close_adder | 0-2 pts | 1 | From R17 session-end evidence |
| rv_high_threshold | 1.5x-3x median | 2x | From FE v2 or rolling calc |
| rv_low_threshold | 0.3x-0.7x median | 0.5x | Conservative |
| threshold_min | 4 pts | 4 | Breakeven floor |
| threshold_max | 8-12 pts | 10 | Maximum before no trades |

**Integration with existing code**:
- Modify `OpportunisticMM.on_stats()` to use dynamic threshold instead of `self._spread_threshold_scaled`
- VPIN signal: Subscribe to `VpinRegimeSwitchStrategy` signal output (already exists)
- Time-of-day: Use `timebase.now_ns()` -> extract wall-clock minutes
- Realized volatility: From FE v2 `ret_autocov_5s_x1e6` (index 17) or compute rolling from tick prices
- All threshold comparisons remain integer (pts * PRICE_SCALE), no float in hot path
- Change is ~30 lines in `on_stats()` method

**Kill criteria**:
1. **No wide-spread regime**: If April TMFD6 data shows < 5% of ticks with spread >= 5 pts, there are too few opportunities. KILL if wide-spread fraction < 5%.
2. **Conditional threshold doesn't help**: If dynamic threshold produces the same trade set as fixed threshold (i.e., spread is bimodal: either 3 pts or 7+ pts, so the threshold value between 4-7 doesn't matter). KILL if trade set overlap > 95%.
3. **VPIN conditioning is noise**: If VPIN-LOW regime trades have the same PnL distribution as VPIN-ELEVATED trades. KILL if PnL difference p > 0.10.
4. **ToD gating redundant**: If CBS ToD filter already captures the same effect. Check correlation with existing CBS session gates.

**Expected edge vs baseline**:
- **Optimistic**: +2-3 bps/trade by (a) capturing some 4-pt spread trades during LOW regime that are currently missed, and (b) avoiding 5-pt spread trades during TOXIC regime that are currently taken.
- **Realistic**: +0.5-1.5 bps/trade. The main value is from the TOXIC regime avoidance (don't trade at spread=5 when VPIN is TOXIC, because adverse selection eats the 1-pt edge).
- **Downside protection**: The threshold_min = 4 (breakeven) floor means we never make expected-loss trades. The fixed threshold of 5 is already safe, so the downside of dynamic thresholds is limited to missed profitable trades during TOXIC regime.

### Candidate C: Combined VPIN-Aware OpMM (Hybrid of A + B)

**Concept**: Instead of full AS-framework gamma scaling (Candidate A), use VPIN regime as ONE input to a lightweight multi-factor gate for OpMM. This combines the best of both directions without the complexity of the full stochastic control framework.

**Algorithm**:
```
def should_quote(spread_pts, vpin_regime, autocov, tob_survival, rv_30s, tod_min):
    """
    Multi-factor activation gate. Returns (should_quote: bool, quote_aggression: int).

    quote_aggression: 0 = standard, +1 = tighter (more aggressive), -1 = wider (conservative)
    """
    # 1. Spread must exceed dynamic breakeven
    dynamic_threshold = compute_dynamic_threshold(vpin_regime, tod_min, rv_30s)
    if spread_pts < dynamic_threshold:
        return False, 0

    # 2. Reversal filter (existing v2 feature gate)
    if autocov >= 0 or tob_survival > 2000:
        return False, 0

    # 3. Aggression scaling based on regime
    if vpin_regime == LOW and rv_30s < rv_median:
        aggression = +1  # tighter quotes, more likely to be hit
    elif vpin_regime == TOXIC:
        aggression = -1  # wider quotes, only fat spreads
    else:
        aggression = 0

    return True, aggression
```

**Integration**: Extends OpMM with ~50 lines. Aggression adjusts the quote offset from mid (tighter = closer to best bid/ask, wider = further). This is simpler than full gamma scaling but captures the same economic intuition.

**Kill criteria**: Same as Candidate B, plus: if aggression adjustment has no measurable effect on fill rate or PnL.

---

## 4. Data Requirements

### Required for all candidates:
1. **TMFD6 L1+L5 tick data**: At least 20 trading days (March 2026 minimum). Available in ClickHouse via `ch_batch_export.py --formats l5`.
2. **VPIN regime labels**: Compute offline from volume bars. Existing `VpinRegimeSwitchStrategy` code provides this.
3. **Spread distribution**: Per-tick spread in points. Must characterize: fraction of time at each spread level (3, 4, 5, 6, 7+ pts).
4. **Time-of-day patterns**: Spread and VPIN regime as a function of wall-clock time.

### Diagnostic data (before prototyping):
- **VPIN regime transition lag**: Measure time between regime change and subsequent adverse price move. If lag > 30s, the signal is too slow.
- **Conditional fill rate**: Fill rate at spread=5 during VPIN-LOW vs VPIN-TOXIC. If no difference, VPIN conditioning is noise.
- **Wide-spread duration**: When spread widens to >=5 pts, how long does it stay wide? If < 200ms, we cannot react at 36ms RTT.

### Estimated data volumes:
- TMFD6 March: ~3M ticks, ~150K L5 snapshots
- VPIN bars: ~2000 bars/day at 500 volume target

---

## 5. Risk Assessment

### Candidate A: VPIN-Conditioned Gamma Scaling

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Base case unviable (spread < breakeven) | CRITICAL | HIGH (March data) | Abort if April repeats March |
| VPIN signal lag (>30s) | HIGH | MEDIUM (R12 evidence) | Measure lag empirically |
| Over-parameterization (gamma_base, alpha, k, tau) | MEDIUM | HIGH | Fix most params, sweep only gamma_base |
| Negligible improvement over spread gate alone | HIGH | HIGH | Compare with Candidate B null hypothesis |
| Implementation complexity (AS framework) | MEDIUM | MEDIUM | Use Gueant closed-form approximation |

**Overall risk**: HIGH. The fundamental obstacle is that this optimizes within a regime (wide spreads) that may not exist frequently enough on TMFD6.

### Candidate B: State-Conditional Spread Threshold

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Spread is bimodal (threshold doesn't matter) | MEDIUM | MEDIUM | Check spread distribution first |
| VPIN conditioning is noise | MEDIUM | MEDIUM | A/B test with random vs VPIN conditioning |
| ToD gating conflicts with CBS | LOW | LOW | Check overlap empirically |
| Threshold below breakeven during LOW regime | LOW | LOW | Floor at 4 pts |
| Insufficient wide-spread observations | MEDIUM | MEDIUM (March) | Require N>=100 trades per condition |

**Overall risk**: MODERATE. The worst case is that dynamic thresholds perform the same as the fixed threshold (no improvement, no harm). The floor at breakeven prevents losses.

### Candidate C: Combined Hybrid

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Complexity without proportional benefit | MEDIUM | MEDIUM | Compare marginal gain vs Candidate B alone |
| Aggression adjustment too small to matter | MEDIUM | HIGH | On discrete LOB, +-1 tick is the minimum |
| Overfitting to multi-factor interaction | MEDIUM | MEDIUM | Walk-forward validation mandatory |

**Overall risk**: MODERATE. Adds marginal complexity over Candidate B with uncertain marginal benefit.

---

## 6. Critical Constraints for TMFD6

### 6.1 The Spread Distribution Problem

From R16 exhaustive analysis:
- **March 2026 median spread**: 3 pts (< 4 pts breakeven)
- **Jan/Feb median spread**: 7 pts (anomalous regime, contract maturity effects)
- **Fraction of time spread >= 5 pts in March**: ~15-25% (estimated)
- **Fraction of time spread >= 7 pts in March**: ~5% (estimated)

This means ANY MM strategy on TMFD6 is opportunistic by nature -- it can only operate during the minority of time when spreads are wide enough. The question is whether we can be smarter about WHICH wide-spread periods to trade.

### 6.2 Latency Constraint

- Shioaji P95 RTT: 36ms
- TMFD6 median tick interval: ~125ms
- Implication: We can react to spread widening within ~1-2 ticks. This is fast enough for regime-level decisions (which change over seconds) but NOT fast enough for individual spread events (which can close in 1 tick).
- Conclusion: The spread threshold approach (react to current spread state) is latency-compatible. Full AS-framework quoting (continuously adjust quotes) is NOT latency-compatible at 36ms.

### 6.3 Fill Rate Reality

From R13/R16 analysis:
- Queue-back adverse selection: When our order is NOT at front of queue, fills are adversely selected (counter-party has more information).
- At 36ms RTT, we are always back-of-queue.
- Implication: Every fill should be assumed adversely selected. The spread edge must compensate for adverse selection, not just RT cost.
- Practical rule: Edge needed = RT cost (4 pts) + adverse selection (1-2 pts) = 5-6 pts minimum spread for profitable trading.
- This SUPPORTS the current threshold of 5 pts and suggests we should NOT lower it to 4 during LOW regime (Candidate B's VPIN-LOW adjustment).

### 6.4 VPIN as Signal vs Noise

From R12 (VPIN as MM overlay: DD -30.6%) and R19 (VPIN = volume intensity proxy):
- VPIN correlates with volume intensity, not with future adverse selection
- Regime transitions are lagging indicators
- As a FILTER (don't trade during TOXIC), VPIN may have value
- As a SIGNAL (trade more during LOW), VPIN is unreliable

This asymmetry is important: VPIN is useful for AVOIDING bad trades but NOT for SEEKING good trades.

---

## 7. Recommendation

### Prototype order: Candidate B first, then Candidate C if B shows promise.

**Rationale**:

1. **Candidate B (Dynamic Threshold)** is the highest-value, lowest-risk option:
   - Implementation: ~30 lines of changes to OpMM
   - Testable: Clear A/B comparison with fixed threshold
   - Downside-protected: Floor at breakeven prevents losses
   - Addresses the known problem: VPIN-TOXIC periods at spread=5 may be losers

2. **Candidate A (Full Gamma Scaling)** should be DEFERRED:
   - The AS framework requires continuous quoting, which conflicts with 36ms latency
   - The gamma parameter is second-order compared to the spread gate (which is binary: quote or don't)
   - R16 showed ALL configs negative on March data -- no gamma value fixes unviable economics
   - If Candidate B shows VPIN conditioning has value, we can later add gamma-like aggression tuning within the gate

3. **Candidate C (Hybrid)** is the natural evolution of Candidate B:
   - If dynamic thresholds show value, add aggression scaling as a refinement
   - The aggression parameter is a simplified version of gamma that respects the discrete LOB

### Specific Stage 2 diagnostic to run first (BEFORE prototyping):

**Diagnostic D1: VPIN-Conditioned Spread Analysis**
```
For each tick where spread >= 5 pts:
  1. Record VPIN regime at that moment
  2. Record price change over next 60s (hold period)
  3. Compute: mean return per regime (LOW / ELEVATED / TOXIC)
  4. If TOXIC regime returns are significantly more negative than LOW regime:
     -> VPIN conditioning has value, proceed with Candidate B
  5. If no significant difference:
     -> VPIN is noise for OpMM, simplify to ToD + volatility only
```

**Diagnostic D2: Wide-Spread Duration Analysis**
```
When spread transitions from < 5 to >= 5:
  1. Measure duration of wide-spread episode (in ms)
  2. If median duration < 200ms: cannot react at 36ms, abort
  3. If median duration > 2s: sufficient time, proceed
  4. Distribution of durations informs whether we need predictive models
```

---

## 8. References

1. Avellaneda, M. & Stoikov, S. (2008). High-frequency trading in a limit order book. *Quantitative Finance*, 8(3), 217-224.
2. Gueant, O., Lehalle, C.-A. & Fernandez-Tapia, J. (2012). Dealing with the Inventory Risk. arXiv:1105.3115.
3. Fodra, P. & Labadie, M. (2012). HF market-making with inventory constraints and directional bets. arXiv:1206.4810.
4. Easley, D., Lopez de Prado, M. & O'Hara, M. (2012). Flow Toxicity and Liquidity in a High-frequency World. *Review of Financial Studies*, 25(5), 1457-1493.
5. Gueant, O. (2016). Optimal Market Making. arXiv:1605.01862.
6. Sarkissian, J. (2016). Spread, volatility, and volume relationship. arXiv:1606.07381.
7. Wyart, M. et al. (2006). Relation between Bid-Ask Spread, Impact and Volatility. arXiv:physics/0603084.
8. Ruan, R., Bacry, E. & Muzy, J.-F. (2023). Self-exciting nature of bid-ask spread dynamics. arXiv:2303.02038.
9. Wang, Z. et al. (2025). Robust Market Making: To Quote, or not To Quote. arXiv:2508.16588.
10. Wang, Z. et al. (2025). ARL-Based Multi-Action Market Making. arXiv:2508.16589.
11. Barzykin, A. et al. (2025). Optimal Quoting under Adverse Selection. arXiv:2508.20225.
12. Gao, X. & Wang, Y. (2018). Optimal Market Making in the Presence of Latency. arXiv:1806.05849.
13. Chavez-Casillas, J. et al. (2024). Adaptive Optimal Market Making. arXiv:2405.11444.
14. Back, K. et al. (2020). Optimal Transport and Risk Aversion in Kyle's Model. arXiv:2006.09518.
15. Cartea, A., Jaimungal, S. & Penalva, J. (2015). *Algorithmic and High-Frequency Trading*. Cambridge University Press.
16. Fang, B. & Feng, Y. (2019). Design of HFT Algorithm Based on Machine Learning. arXiv:1912.10343.

---

## Appendix: Avellaneda-Stoikov Key Formulas (for reference)

The canonical AS market-making framework:

**Reservation price** (the MM's private fair value given inventory q):
```
r(t, q) = S(t) - q * gamma * sigma^2 * (T - t)
```

**Optimal bid/ask distances from reservation price**:
```
delta_bid = delta_ask = (1/gamma) * ln(1 + gamma/k) + (1/2) * gamma * sigma^2 * (T-t)
```

Where the first term (1/gamma * ln(1+gamma/k)) is the "spread for liquidity" component and the second term is the "spread for inventory risk" component.

**Simplified Gueant approximation** (practical):
```
delta* ~ 1/k + gamma * sigma^2 * tau / 2
```

**Inventory skew** (Fodra-Labadie):
```
delta_bid = delta* - q * eta * theta_2(t)    # tighter bid when long
delta_ask = delta* + q * eta * theta_2(t)    # wider ask when long
```

For TMFD6 with gamma ~ 0.01, sigma ~ 0.0005 (30s), tau ~ 60s:
- gamma * sigma^2 * tau / 2 ~ 0.01 * 2.5e-7 * 60 / 2 ~ 7.5e-8
- This is negligible compared to the tick size (1 pt = 0.0001 in scaled terms)
- **Conclusion**: The gamma-dependent spread adjustment is sub-tick on TMFD6. This confirms that Candidate A (full gamma scaling) adds negligible value on this instrument.
