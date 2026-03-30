# Stage 1: Literature Survey -- MLOFI-driven Micro-Price Adjustment

**Researcher**: researcher
**Date**: 2026-03-27
**Direction**: 4.2.1 MLOFI-driven Micro-Price Adjustment
**Status**: COMPLETE -- 3 candidates proposed, 1 recommended for prototype

---

## 1. Paper Summaries

### P1. Stoikov (2018) -- "The Micro-Price: A High-Frequency Estimator of Future Prices"
*Quantitative Finance 18(12):1959-1966*

Defines the **micro-price** as a martingale estimator of the "true" price, conditioned on
L1 order book state (spread S and volume imbalance I). The weighted mid-price
`P_w = I * P_a + (1-I) * P_b` (where `I = Q_b / (Q_b + Q_a)`) is the simplest version.
The full Stoikov micro-price is constructed recursively: given current state (I, S),
compute the probability of all next-state transitions using historical data, and take
the expectation over all paths that end with a mid-price change. The recursion converges
in ~6 steps. Key result: micro-price is a strictly better predictor of the next mid-price
change than weighted mid or raw mid. Limitation: uses **only L1 data** -- deeper book
information is ignored entirely.

**Relevance**: Our FeatureEngine already computes `microprice_x2` using the weighted-mid
formula (L1 imbalance only). The Stoikov recursive method is richer but requires
pre-computed transition matrices -- feasible for 2330 with enough historical data.

### P2. Xu, Gould & Howison (2019) -- "Multi-Level Order-Flow Imbalance in a Limit Order Book"
*arXiv:1907.06230*

Introduces **MLOFI** as a vector quantity measuring net buy/sell flow at each price level.
Fits the linear model `Delta_mid = beta^T * MLOFI` where beta is estimated via OLS or
ridge regression. Key finding: out-of-sample R-squared improves monotonically as more
levels are added (up to L10 tested). For large-tick Nasdaq stocks, adding levels 2-10
reduces RMSE by 68-74%; for small-tick stocks, 15-31%. Due to strong inter-level
correlation, PCA on the MLOFI vector produces a single "integrated MLOFI" that captures
most variance. The first principal component weights decay roughly geometrically with depth.

**Relevance**: We already have the MLOFI gradient concept from Round 11 (IC=-0.105 on 2330).
The key insight for micro-price adjustment is that the MLOFI regression coefficients
(beta vector) provide the theoretically-grounded weighting for multi-level fair value
correction. On TWSE, we know deep-book activity is passive (inverted sign), so the beta
signs would differ from Nasdaq.

### P3. Cont, Cucuringu & Zhang (2023) -- "Cross-Impact of Order Flow Imbalance in Equity Markets"
*arXiv:2112.13213, published Quant. Finance 2023*

Extends MLOFI to cross-asset setting. Proposes systematic combination of OFIs at top-K
levels into an **integrated OFI** via PCA. The integrated OFI explains price impact better
than L1-only OFI both in-sample and out-of-sample. Lagged cross-asset OFIs improve
return forecasting. Key empirical finding: once multi-level information is integrated,
cross-asset contemporaneous impact adds little -- the depth information is more valuable
than breadth.

**Relevance**: Validates that multi-level OFI integration is the critical axis of
improvement for price estimation, not cross-asset effects. Our L5 equity data for 2330
is well-suited. For futures (TXFD6/TMFD6), we only have L1, so the cross-asset angle
(equity L5 -> futures micro-price) becomes the relevant application path.

### P4. Pulido, Rosenbaum & Sfendourakis (2023) -- "Understanding the Worst-Kept Secret of HFT"
*arXiv:2307.15599*

Provides the theoretical foundation for **why** volume imbalance predicts price moves.
The weighted mid-price `P_w = (I+1)/2 * P_a + (1-I)/2 * P_b` is derived as the
conditional expectation of the next mid-price change given imbalance. Proves that the
micro-price is a martingale by construction, which is critical for MM fair-value usage
(no systematic drift means no inherent adverse selection from the estimator itself).

**Relevance**: Provides the theoretical guarantee that micro-price adjustments based on
imbalance do not introduce systematic bias into MM quoting -- essential for our MM
strategy framework.

### P5. Berild, Lei & Granmo (2024) -- "High Resolution Microprice Estimates from LOB Data"
*arXiv:2411.13594*

Proposes an **error-correcting model** for the micro-price: start with the standard
Stoikov microprice conditioned on (I, S), then add a correction term based on
**higher-rank imbalances** (L2-L5+). The correction is learned via a Tsetlin Machine
for fast evaluation. Demonstrates that the error correction from deeper levels provides
a statistically significant improvement in predicting next mid-price moves beyond what
L1 micro-price captures alone. Tsetlin Machine evaluation is extremely fast (~nanoseconds),
making it suitable for HFT deployment.

**Relevance**: This is the closest paper to our proposed direction. It explicitly combines
L1 micro-price with multi-level depth corrections. The architecture (base microprice +
OFI correction) maps directly to our candidate formulas below.

### P6. Barzykin, Bergault, Guéant & Lemmel (2025) -- "Optimal Quoting under Adverse Selection and Price Reading"
*arXiv:2508.20225*

Extends Cartea-Jaimungal MM framework to account for adverse selection (informed flow)
and price reading (information leakage from inventory). Derives first-order adjustments
to optimal quotes that depend on flow toxicity signals. The key insight for us: the
optimal bid/ask offset from fair value depends on the **adverse selection intensity**,
which can be estimated from order flow asymmetry -- exactly what MLOFI captures.

**Relevance**: Provides the theoretical link between MLOFI-based adverse selection
detection and quote width adjustment. Not just micro-price correction, but also
**quote width modulation** based on detected flow toxicity.

---

## 2. Candidate Alpha Directions

### Candidate A: `mlofi_microprice_correction` -- Linear MLOFI Correction to Weighted Mid

**Formula**:
```
micro_price_adj = weighted_mid + alpha * MLOFI_integrated

where:
  weighted_mid = (Q_b * P_a + Q_a * P_b) / (Q_b + Q_a)     [already: microprice_x2]
  MLOFI_L_k = delta_bid_qty_k - delta_ask_qty_k              (per-level OFI, k=1..5)
  MLOFI_integrated = sum_{k=1}^{5} w_k * MLOFI_L_k           (geometrically weighted)
  w_k = lambda^(k-1), lambda in [0.3, 0.7]                   (fit from data)
  alpha = regression coefficient (bps per unit MLOFI)         (fit from data)
```

**TWSE-specific sign convention**: On TWSE, MLOFI gradient is contrarian (IC=-0.105 means positive
gradient => price DOWN). Therefore the correction sign is **negative**: when deep book replenishes
(positive MLOFI gradient), fair value should be adjusted DOWN because informed flow is selling at L1.

**Hypothesis**: The weighted mid-price using L1 imbalance alone misses 15-70% of the
price-predictive information in the order book (per Xu et al. 2019). Adding the MLOFI
correction captures the L2-L5 signal that is already available in our FeatureEngine data
but currently unused by MM strategies. The correction acts as a "toxicity-aware fair value"
that shifts quotes away from incoming informed flow.

**Data requirement**:
- 2330 (TSMC): L5 depth available, ideal test case. 2.17M ticks, 11 days.
- TXFD6: L5 depth available via `BidAskEvent.bids/asks` shape (5,2).
- TMFD6: **L1 only** -- this candidate CANNOT be used for TMFD6 directly.

**Fee breakeven**:
- TWSE 2330: RT cost ~58.5 bps. Signal is for MM quote placement, not directional trading.
  The value is in REDUCING adverse selection (fewer fills on wrong side), not in generating
  standalone returns. Breakeven: improve fill quality by > 0.5 pts per fill on average.
- TXFD6: RT cost ~6 bps. MLOFI correction of ~2-3 bps shift in fair value is actionable.
  Need IC > 0.03 at 30-second horizon to break even.

**Risk**:
1. **Overfitting regression weights**: MLOFI weights (lambda, alpha) calibrated on 11 days
   may not be stable. Mitigation: geometric decay prior (only 1 free parameter).
2. **TWSE inverted microstructure**: US-calibrated papers assume deep=informed. On TWSE,
   deep=passive. Must validate sign empirically before deployment.
3. **Signal half-life vs latency**: If MLOFI correction has < 100ms half-life, the 36ms
   API RTT consumes a large fraction of the edge. Prior work (IC at ~125ms tick cadence)
   suggests half-life is 1-5 seconds -- comfortably above RTT.

---

### Candidate B: `adverse_flow_quote_width` -- MLOFI-driven Dynamic Quote Width

**Formula**:
```
quote_half_spread = base_half_spread + gamma * |MLOFI_integrated|

where:
  base_half_spread = max(min_tick, current_spread / 2)       [existing in simple_mm.py]
  MLOFI_integrated = same as Candidate A
  gamma = width sensitivity (scaled pts per unit |MLOFI|)    (fit from data)

Direction: MLOFI sign determines which side widens more:
  if MLOFI_integrated > 0 (passive refill = informed selling on TWSE):
    bid_offset = base + gamma * |MLOFI|      (widen bid -- toxic buy flow)
    ask_offset = base                         (keep ask tight -- safe to sell)
  else:
    bid_offset = base                         (keep bid tight)
    ask_offset = base + gamma * |MLOFI|      (widen ask -- toxic sell flow)
```

**Hypothesis**: Rather than shifting the fair value point estimate, modulate **quote width
asymmetrically** based on detected adverse flow direction. When MLOFI indicates incoming
toxic flow from one side (L1 aggression with L3-L5 passive refill on the SAME side),
widen quotes on that side to reduce adverse selection fills. This is the direct application
of Barzykin et al. (2025) to our context.

**Data requirement**: Same as Candidate A -- requires L5 depth. Applicable to 2330 and TXFD6.

**Fee breakeven**:
- The value is measured as adverse selection reduction: fewer fills at unfavorable prices.
  Target: reduce adverse-fill rate by > 5% (from ~50% baseline measured in Round 18 SG-LP).
- TXFD6: If each avoided adverse fill saves 2 pts (1 tick), and 5% of ~460 fills/session
  are saved, that is ~46 pts/session = 9,200 NTD.

**Risk**:
1. **Overly defensive quotes**: Widening quotes too aggressively reduces fill rate and
   spread capture. Must balance fill rate vs adverse selection.
2. **Latency of response**: By the time we detect adverse MLOFI and adjust quotes, the
   informed flow may have already moved the price. 36ms RTT is the binding constraint.
3. **Regime instability**: The MLOFI-toxicity relationship may vary by time of day and
   volatility regime. Morning (momentum) vs afternoon (mean-reversion) may require
   different gamma values.

---

### Candidate C: `cross_level_microprice` -- Stoikov Microprice + Multi-Level Error Correction

**Formula**:
```
micro_price_v2 = stoikov_microprice(I_L1, S) + epsilon(I_L2, I_L3, I_L4, I_L5)

where:
  stoikov_microprice(I, S) = mid + g(I, S)
    g(I, S) = pre-computed lookup table from transition matrix (6-step recursion)
    I_L1 = (Q_b1 - Q_a1) / (Q_b1 + Q_a1)  [L1 imbalance]
    S = spread in ticks

  epsilon(I_L2..L5) = sum_{k=2}^{5} beta_k * I_Lk
    I_Lk = (Q_bk - Q_ak) / (Q_bk + Q_ak)  [per-level imbalance]
    beta_k = regression coefficients (pre-fit, decaying with k)

Optionally: epsilon includes cross-terms:
  epsilon += delta * (I_L1 - I_avg_deep) * |S|
  where I_avg_deep = mean(I_L2..I_L5)
  This captures the "shallow-deep divergence" signal from ofi_depth_divergence alpha.
```

**Hypothesis**: Combines the theoretically optimal L1 micro-price (Stoikov) with the
multi-level error correction (Berild et al. 2024). The cross-term explicitly captures
the TWSE "deep replenishment = reversal" finding from our Round 11/Round 15 research.
This is the most academically grounded approach but also the most complex.

**Data requirement**:
- Requires historical L5 data to pre-compute the Stoikov transition matrix AND the
  multi-level beta coefficients.
- 2330: 2.17M ticks (11 days) -- marginal for transition matrix stability.
- TXFD6: L5 available but tick structure differs (futures vs equities).
- TMFD6: **NOT applicable** (L1 only).

**Fee breakeven**: Same as Candidate A -- this is a fair-value improvement for MM, not
a standalone directional alpha. Value is measured by fill-quality improvement.

**Risk**:
1. **Complexity**: Two pre-computed models (transition matrix + regression) require more
   data and more maintenance than the linear approaches.
2. **Lookup table staleness**: The Stoikov transition matrix may shift over time as market
   microstructure changes (contract rolls, volatility regimes).
3. **Diminishing returns**: Berild et al. (2024) show that L2-L3 provide most of the
   correction; L4-L5 add noise for many instruments. Our prior finding that L3-L5
   add noise on TWSE (Round 15) is consistent.

---

## 3. Data Availability Assessment

| Asset | LOB Depth | Ticks Available | Days | MLOFI Feasible | Microprice Candidate |
|-------|-----------|-----------------|------|----------------|---------------------|
| 2330 (TSMC) | L5 | 2.17M | 11 | Yes | A, B, C |
| TXFD6 | L5 (via BidAskEvent) | ~2M+ | 11+ | Yes | A, B, C |
| TMFD6 | L1 only | ~6.3M | 22 | No (L1 only) | None directly |
| Other TWSE equities | L5 | varies | varies | Yes (if subscribed) | A, B, C |

**Key data gap**: TMFD6 (our active strategy target for CBS and OpMM) has only L1 data.
MLOFI-based micro-price adjustment is structurally inapplicable to TMFD6 unless we:
1. Use TXFD6 as a L5 proxy (same underlying, but TMFD6 is mini contract), OR
2. Use 2330 L5 MLOFI as a cross-asset signal for TMFD6 (equity -> futures lead-lag)

Option 1 is more tractable -- TXFD6 L5 MLOFI could adjust the fair value for TMFD6
quoting, since they track the same TAIEX index.

**FeatureEngine readiness**: The `microprice_x2` (L1 weighted mid) is already computed as
feature index 7 in `lob_shared_v2`. The `ofi_l1_raw`, `ofi_l1_ema8`, and
`ofi_depth_norm_ppm` features provide the L1 OFI foundation. Adding a multi-level MLOFI
correction would require a new feature (e.g., `mlofi_microprice_adj_x2` at index 21).

---

## 4. Recommendation

### Prototype first: **Candidate A (`mlofi_microprice_correction`)**

**Rationale**:
1. **Simplest formula**: One regression coefficient (alpha) + one decay parameter (lambda).
   Easy to validate and debug.
2. **Direct use of existing infrastructure**: MLOFI gradient concept already validated in
   Round 11 (IC=-0.105). The linear correction is the natural next step.
3. **Clear TWSE adaptation**: We already know the sign is inverted. The linear model makes
   the sign flip trivial (just negate alpha).
4. **Applicable to both equities and TXFD6**: We have L5 data for both. If it works on
   2330, we can immediately test on TXFD6 for MM strategies.
5. **Low data requirement**: 11 days is sufficient for a 2-parameter linear model.

### Prototype second: **Candidate B (`adverse_flow_quote_width`)**

**Rationale**: Candidate B is orthogonal to Candidate A (fair value shift vs width
modulation). If Candidate A improves fair value but Candidate B improves fill selection,
they can be combined. B also has a direct path to production via the existing
`OpportunisticMM` spread gate mechanism.

### Defer: **Candidate C (`cross_level_microprice`)**

**Rationale**: Candidate C requires pre-computed transition matrices and more data than we
currently have. The marginal improvement over Candidate A is likely small (Round 15 showed
L3-L5 add noise on TWSE). Revisit only if Candidate A shows the linear correction is
insufficient and nonlinear state-dependent correction is needed.

---

## 5. Constraints and Execution Notes

### Latency Budget
- Shioaji P95 RTT: **36ms** (submit), **43ms** (modify), **47ms** (cancel)
- Local pipeline: ~250us
- MLOFI computation: O(1) per tick (delta from prior snapshot), ~1us
- Signal half-life: estimated 1-5 seconds based on MLOFI gradient IC persistence
- **Verdict**: Signal half-life >> API RTT. Executable with comfortable margin.

### TWSE Microstructure Inversion
All candidate formulas must account for:
- Deep book activity (L3-L5) = passive replenishment, NOT informed accumulation
- MLOFI gradient positive = passive refill = price reversal (contrarian signal)
- This is **inverted** vs US equity (Cont et al. 2023, Xu et al. 2019)
- The regression coefficients (alpha, beta_k) will have opposite signs from US calibration

### MM Integration Path
The adjusted micro-price feeds into the existing MM framework at line 48 of `simple_mm.py`:
```python
# Current: micro_price_x2 = mid_price_x2 + imbalance_adj  (L1 only)
# Proposed: micro_price_x2 = mid_price_x2 + imbalance_adj + mlofi_correction
```
Where `mlofi_correction` is the scaled integer output of the MLOFI linear model.
The `OpportunisticMM` strategy can consume this via the FeatureEngine event bus.

### Kill Gates for Stage 2
- **IC gate**: MLOFI correction must show IC > 0.03 at 30-second horizon on 2330 L5
- **Stability gate**: Coefficient signs must be consistent across all 11 available days
- **Half-life gate**: Signal half-life > 500ms (confirmed via autocorrelation decay)
- **TWSE sign gate**: Deep-level coefficients must be contrarian (negative beta for L3-L5)
- **Integration gate**: Adjusted micro-price must be a better predictor than L1-only
  microprice_x2 (out-of-sample R-squared improvement > 5%)

---

## References

1. Stoikov, S. (2018). "The micro-price: a high-frequency estimator of future prices."
   *Quantitative Finance*, 18(12), 1959-1966.
2. Xu, K., Gould, M.D. & Howison, S.D. (2019). "Multi-Level Order-Flow Imbalance in a
   Limit Order Book." arXiv:1907.06230.
3. Cont, R., Cucuringu, M. & Zhang, C. (2023). "Cross-Impact of Order Flow Imbalance in
   Equity Markets." arXiv:2112.13213. *Quantitative Finance*, 2023.
4. Pulido, S., Rosenbaum, M. & Sfendourakis, E. (2023). "Understanding the Worst-Kept
   Secret of High-Frequency Trading." arXiv:2307.15599.
5. Berild, M.O., Lei, J. & Granmo, O.-C. (2024). "High Resolution Microprice Estimates
   from Limit Orderbook Data using Hyperdimensional Vector Tsetlin Machines."
   arXiv:2411.13594.
6. Barzykin, A., Bergault, P., Guéant, O. & Lemmel, M. (2025). "Optimal Quoting under
   Adverse Selection and Price Reading." arXiv:2508.20225.
7. Internal: Round 11 MLOFI gradient research (IC=-0.105, Gate C FAIL, reclassified as
   FeatureEngine v2 feature).
8. Internal: Round 15 LOB Kinetic Energy (L3-L5 add noise on TWSE).
9. Internal: Round 18 SG-LP adverse selection analysis (~50% adverse fills at baseline).
