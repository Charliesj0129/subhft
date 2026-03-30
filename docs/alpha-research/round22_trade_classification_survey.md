# Round 22: Trade Classification Survey — Inferring Buy/Sell Direction from Tick Data

**Date**: 2026-03-28
**Objective**: Survey methods for inferring trade direction (buy/sell classification) from tick data without explicit trade tags, and assess what becomes possible once classified trades are available.
**Context**: TAIFEX futures tick data provides price, volume, timestamp but NO buy/sell tag. We have concurrent L1-L5 bid/ask snapshots.

---

## Executive Summary

Trade classification is the process of labeling each transaction as buyer-initiated or seller-initiated. This is a prerequisite for computing signed order flow, VPIN, toxic flow metrics, and Hawkes branching ratios -- all signals that prior research rounds (R12, R16, R17, R19, R20) identified as "blocked by data gap."

**Key findings from this survey:**

1. **Lee-Ready with concurrent bid/ask data achieves 85-93% accuracy** on equities. Since we have L1-L5 bid/ask snapshots concurrent with trades, this is directly implementable.
2. **Tick rule alone achieves 77-91%** and requires NO bid/ask data, but degrades on large-tick assets (our case).
3. **Bulk Volume Classification (BVC) achieves only ~80%** and has been shown to produce spurious VPIN signals (Andersen & Bondarenko 2014).
4. **For large-tick futures like TMFD6/TXFD6, classification accuracy degrades** because many trades occur at the quote midpoint or at a single tick level, making the quote rule ambiguous.
5. **ML-based methods (gradient boosting, neural networks) using order book features can reach 90-95%** accuracy but require labeled training data (which we lack for TAIFEX).
6. **A pragmatic approach for TAIFEX**: Implement Lee-Ready using our L5 bid/ask snapshots, validate accuracy using known properties of signed OFI (autocorrelation, price impact linearity), and use the classified trades to unlock signed OFI, Hawkes, and toxic flow signals.

---

## 1. Classical Trade Classification Algorithms

### 1.1 The Quote Rule (Lee & Ready 1991)

**Paper**: Lee, C.M.C. and Ready, M.J. (1991). "Inferring Trade Direction from Intraday Data." *Journal of Finance*, 46(2), 733-746.

**Methodology**:
- Compare trade price to prevailing quote midpoint: `mid = (best_bid + best_ask) / 2`
- If `trade_price > mid` => buyer-initiated (aggressive buy lifted the ask)
- If `trade_price < mid` => seller-initiated (aggressive sell hit the bid)
- If `trade_price == mid` => ambiguous, fall back to tick rule

**Accuracy**: 85% overall (Odders-White 2000, NYSE data). Up to 93% for trades clearly at bid or ask.

**Applicability to TAIFEX**:
- **DIRECTLY APPLICABLE**: We have concurrent L1-L5 bid/ask snapshots
- **Concern**: TMFD6 median spread = 3 points, and many trades occur AT the bid or ask (not between). For large-tick assets where spread = 1 tick, nearly all trades are at bid or ask, making quote rule highly effective for those specific trades
- **Concern**: Trades AT the midpoint are unclassifiable by quote rule alone (requires tick rule fallback)

### 1.2 The Tick Rule (Tick Test)

**Paper**: Widely used since Hasbrouck (1988); formalized in multiple studies.

**Methodology**:
- Compare current trade price to previous trade price
- Uptick (price > prev_price) => buyer-initiated
- Downtick (price < prev_price) => seller-initiated
- Zero-tick => use direction of last non-zero tick change

**Accuracy**:
- Equities: 77-83% (Ellis, Michaely, O'Hara 2000; Theissen 2001)
- Futures: ~85% (varies by contract and tick regime)
- Bitcoin: Similar accuracy reported (Augustin et al. 2021)

**Applicability to TAIFEX**:
- **DIRECTLY APPLICABLE**: Requires only price series (which we have)
- **Weakness**: Degrades significantly for large-tick assets. When tick size = minimum price increment, many consecutive trades occur at the same price (zero-ticks), making the rule rely on increasingly stale direction information
- **Strength**: No bid/ask data needed; can serve as fallback for midpoint trades

### 1.3 The Lee-Ready Algorithm (Combined Quote + Tick)

**Methodology**:
1. First apply quote rule (compare to midpoint)
2. For trades at the midpoint, apply tick rule
3. Original paper suggested 5-second quote delay; modern implementations use contemporaneous quotes

**Accuracy**:
- NYSE: 85% overall (Odders-White 2000)
- NASDAQ: 81% (Ellis, Michaely, O'Hara 2000)
- Frankfurt Stock Exchange: 72.8% (Theissen 2001)
- With ITCH data (true aggressor known): 92.6% (Chakrabarty et al. 2015)

**Key insight**: The 5-second delay is an artifact of the TAQ database era. With modern, synchronized tick-level data (which we have), contemporaneous quotes should be used.

### 1.4 The EMO Algorithm (Ellis, Michaely, O'Hara 2000)

**Paper**: Ellis, K., Michaely, R., and O'Hara, M. (2000). "The Accuracy of Trade Classification Rules: Evidence from Nasdaq." *JFQA*, 35(4), 529-551.

**Methodology**:
- Classify at the ask => buyer-initiated
- Classify at the bid => seller-initiated
- Otherwise => tick rule
- No quote midpoint comparison needed

**Accuracy**: 81.05% on NASDAQ (comparable to Lee-Ready)

**Applicability to TAIFEX**:
- Very suitable for large-tick futures where most trades occur AT bid or ask
- Simple to implement with our bid/ask data
- May outperform Lee-Ready when spread = 1 tick (common for TMFD6)

### 1.5 The CLNV Algorithm (Chakrabarty, Li, Nguyen, Van Ness 2007)

**Paper**: Chakrabarty, B., Li, B., Nguyen, V., Van Ness, R. (2007). "Trade Classification Algorithms for Electronic Communications Network Trades." *Journal of Banking & Finance*, 31(12), 3806-3821.

**Methodology**:
- Trade at ask => buyer-initiated
- Trade at bid => seller-initiated
- Trade price within 30% of spread from ask => buyer-initiated
- Trade price within 30% of spread from bid => seller-initiated
- Otherwise => tick rule

**Accuracy**: Improves on EMO for ECN trades; ~83-87% overall

**Applicability to TAIFEX**:
- The 30% spread zone is meaningful only when spread > 1 tick
- For TMFD6 with 3-point median spread, this creates useful intermediate zones
- Worth testing against EMO

---

## 2. Modern and Advanced Methods

### 2.1 The Full-Information (FI) Algorithm (Jurkatis 2020)

**Paper**: Jurkatis, S. (2020). "Inferring Trade Directions in Fast Markets." Bank of England Working Paper No. 896. Published in *Journal of Financial Markets*, 58, 2022.
**SSRN**: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3748290
**Code**: https://github.com/jktis/Trade-Classification-Algorithms

**Methodology**:
- Key insight: In fast markets, the "prevailing quote" at trade time is ambiguous due to timestamp precision and rapid quote changes
- The FI algorithm actively searches for the quote that matches a trade by examining the full sequence of quote updates around the trade timestamp
- Uses all available information (hence "full-information") rather than relying on the last quote before the trade

**Accuracy**:
- For data timestamped to the second: correctly classifies **95%** of trading volume, vs 90% for EMO (best competitor)
- Reduces misclassification by **half** compared to Lee-Ready at low timestamp precision
- Performance advantage grows as timestamp precision decreases

**Applicability to TAIFEX**:
- **HIGHLY RELEVANT**: Our TAIFEX data has good timestamp precision (sub-second), but quote changes can be rapid
- The FI algorithm's approach of searching for matching quotes is particularly valuable when quote and trade timestamps are not perfectly synchronized
- Open-source Python implementation available on GitHub

### 2.2 Bulk Volume Classification (BVC) (Easley, Lopez de Prado, O'Hara 2012)

**Paper**: Easley, D., Lopez de Prado, M., O'Hara, M. (2012). "Bulk Classification of Trading Activity." Working paper.

**Methodology**:
- Instead of classifying individual trades, classify volume in time/volume bars
- Use a CDF-based probabilistic split: `V_buy = V * CDF(Z)` where `Z = dP / sigma`
- dP = price change over bar, sigma = estimated volatility
- No bid/ask data needed

**Accuracy**:
- At 1-hour bars: ~80% (Chakrabarty et al. 2015)
- At 1-minute bars: higher, approaching tick-level methods
- Significantly worse than tick rule and Lee-Ready across the board

**Critical Criticism (Andersen & Bondarenko 2014)**:
- BVC accuracy is inferior to standard tick rule
- VPIN constructed from BVC has predictive power for volatility ONLY because BVC generates systematic classification errors correlated with volume and volatility
- When controlling for trading intensity, BVC-VPIN has NO incremental predictive power
- **"VPIN is unsuitable for capturing order flow toxicity"** -- a devastating finding

**Applicability to TAIFEX**:
- **NOT RECOMMENDED** as primary classifier given the Andersen-Bondarenko critique
- However, BVC has one advantage: it works without bid/ask data and is computationally trivial
- If used at all, should be validated against tick-rule-based VPIN
- Panayides, Shohfi, Smith (2019) partially rehabilitated BVC for Euronext Paris data, finding it "superior with respect to data efficiency and ability to capture informative trade flow" -- but this was for a specific market microstructure

### 2.3 Machine Learning Approaches

#### 2.3.1 Gradient Boosting for Trade Classification

**Key reference**: Multiple studies using XGBoost/LightGBM with order book features.

**Feature engineering for ML trade classification**:
- Bid-ask spread at time of trade
- Order book imbalance (bid_size - ask_size) / (bid_size + ask_size) at L1-L5
- Price position within the spread: (trade_price - bid) / (ask - bid)
- Recent price momentum (last N ticks)
- Volume at bid vs ask
- Depth-weighted mid price
- Time since last trade
- Recent trade direction sequence

**Accuracy**: 90-95% reported in studies with labeled data (ITCH, direct exchange feeds)

**Challenge for TAIFEX**: Requires labeled training data. We do NOT have ground-truth trade direction labels. However, there is a bootstrap approach:
1. Label "easy" trades using quote rule (clearly at bid or ask) as training data
2. Train ML model on these high-confidence labels
3. Use model to classify ambiguous trades (at midpoint, during fast markets)

#### 2.3.2 Deep Learning on Order Book Data

**Key references**:
- Zhang et al. (2019). "DeepLOB: Deep Convolutional Neural Networks for Limit Order Books." arXiv:1808.03668
- Kolm, Turiel, Westray (2024). "Deep Limit Order Book Forecasting: A Microstructural Guide." arXiv:2403.09267

**Methodology**: CNN/LSTM architectures processing raw L5 order book snapshots to predict trade direction or price movement.

**Applicability to TAIFEX**:
- We have L5 data, so the input format matches
- However, these models predict price DIRECTION, not trade CLASSIFICATION
- Could potentially be adapted: train to predict "was this trade buyer or seller initiated?" using order book state as features
- Requires labeled training data (same bootstrap challenge as gradient boosting)

---

## 3. Accuracy Comparison Summary

| Algorithm | Equities Accuracy | Futures Accuracy | Requires Bid/Ask | Requires Labels | Complexity |
|-----------|------------------|------------------|-------------------|-----------------|------------|
| **Tick Rule** | 77-83% | 85% | No | No | O(1) |
| **Quote Rule** | 85-93% | 85-90% | Yes (L1) | No | O(1) |
| **Lee-Ready** | 85-93% | 85-90% | Yes (L1) | No | O(1) |
| **EMO** | 81% | ~85% | Yes (L1) | No | O(1) |
| **CLNV** | 83-87% | ~87% | Yes (L1) | No | O(1) |
| **FI (Jurkatis)** | 95% | ~92% | Yes (full book) | No | O(n) |
| **BVC** | ~80% | ~80% | No | No | O(1) |
| **ML (XGBoost)** | 90-95% | 90-95% | Yes (L1-L5) | Yes | O(n) |

**Notes on futures-specific accuracy**:
- Futures markets tend to have simpler microstructure (centralized exchange, no fragmentation)
- Large-tick futures (like TMFD6 where spread often = 1 tick) actually make quote rule MORE accurate for non-midpoint trades
- But many trades cluster at a single price level, increasing the proportion of ambiguous (midpoint) trades
- Net effect: accuracy is similar to equities but with different error distribution

---

## 4. The Large-Tick Problem (Critical for TAIFEX)

TMFD6 and TXFD6 are **large-tick assets** where the minimum tick size is a significant fraction of the typical spread. This creates specific challenges:

### 4.1 The Problem

**Reference**: Dayri, K. and Rosenbaum, M. (2012). "Large tick assets: implicit spread and optimal tick size." arXiv:1207.6325

- When spread = 1 tick (common for TMFD6), the midpoint falls BETWEEN the only two possible price levels
- ALL trades must occur at either bid or ask (there is no "between" price)
- This means the **quote rule is actually very accurate** for these trades -- the ambiguous "at midpoint" case rarely occurs
- However, when multiple trades occur at the same price, the tick rule degrades

### 4.2 Implication for Our Implementation

For TMFD6 (median spread = 3 points = 3 ticks):
- Many trades occur at bid or ask => quote rule works well (~90%+ accuracy)
- Some trades occur inside the spread => quote rule with midpoint comparison works
- Very few trades at exact midpoint => tick rule fallback needed for small minority

For TXFD6 (tighter spread, often 1 tick):
- Nearly all trades at bid or ask => quote rule extremely accurate
- Zero-tick sequences are common => tick rule provides little help

**Recommendation**: Implement EMO (at-bid/at-ask first, tick rule fallback) rather than Lee-Ready for large-tick futures. The midpoint comparison adds little value when the spread is 1-3 ticks.

---

## 5. What Classification Accuracy is "Good Enough"?

### 5.1 Impact of Misclassification on Downstream Signals

**Key reference**: Boehmer, E., Grammig, J., Theissen, E. (2007). "Estimating the Probability of Informed Trading -- Does Trade Misclassification Matter?" *Journal of Financial Markets*, 10(1), 26-47.

**Findings**:
- Misclassification introduces **downward bias** in PIN estimates
- Even 85% accuracy is sufficient for most microstructure studies
- The bias is approximately proportional to `(1 - accuracy)^2`, so going from 85% to 90% has diminishing returns
- For OFI computation, random misclassification adds noise but does NOT introduce systematic bias (the errors are approximately symmetric between buy and sell)

### 5.2 Theoretical Analysis

For signed OFI = sum of signed_volume_i:
- True sign: +1 (buy) or -1 (sell)
- With accuracy p, expected sign = p * true_sign + (1-p) * (-true_sign) = (2p-1) * true_sign
- So OFI is attenuated by factor **(2p - 1)**:
  - p = 85% => attenuation = 0.70 (30% signal loss)
  - p = 90% => attenuation = 0.80 (20% signal loss)
  - p = 95% => attenuation = 0.90 (10% signal loss)
- This is an **attenuation bias**, not a directional bias -- the sign of OFI is preserved

### 5.3 Practical Threshold

For our use cases:
- **Signed OFI / Order Flow Imbalance**: 85% accuracy is sufficient. Signal is attenuated by ~30% but direction is correct.
- **VPIN**: 85% is marginal. VPIN is sensitive to classification accuracy (Andersen & Bondarenko showed BVC errors create spurious predictions). Use tick-rule-based classification, not BVC.
- **Hawkes branching ratio**: 85% is sufficient for estimating endogeneity. Random misclassification adds noise to inter-arrival time estimation but does not bias the branching ratio systematically.
- **Toxic flow (adverse selection)**: 90%+ preferred. Adverse selection metrics compare fills against subsequent price moves; misclassified direction weakens the signal.

---

## 6. What Becomes Possible with Classified Trades

### 6.1 Signed Order Flow Imbalance (OFI)

**Reference**: Cont, R., Kukanov, A., Stoikov, S. (2014). "The Price Impact of Order Book Events." *Journal of Financial Econometrics*, 12(1), 47-88. arXiv:1011.6402

Currently, our OFI is computed from order book changes only (depth changes at bid/ask). With trade classification, we can compute:
- **Trade-signed OFI**: Sum of (signed_volume * direction) per time interval
- **Multi-level signed OFI**: Combine L1-L5 depth changes with trade-level aggressor side
- **Cross-asset signed flow**: Correlate TSMC stock signed flow with TMFD6 futures signed flow

**Expected impact**: Prior rounds showed unsigned OFI has IC ~0.01-0.03. Signed OFI typically has 2-3x higher IC because it separates informed from uninformed flow.

### 6.2 VPIN (Volume-Synchronized Probability of Informed Trading)

**Reference**: Easley, D., Lopez de Prado, M., O'Hara, M. (2012). "Flow Toxicity and Liquidity in a High Frequency World." *Review of Financial Studies*, 25(5), 1457-1493.

With classified trades:
- Compute VPIN using tick-rule classification (NOT BVC) following Andersen-Bondarenko recommendations
- Use as a regime indicator: high VPIN => elevated informed trading => widen spreads or reduce size
- R12 showed VPIN as overlay caused -30.6% DD -- but that used BVC. Tick-rule VPIN may behave differently.

**Caution**: Andersen & Bondarenko (2014) showed that VPIN's predictive power is largely an artifact of BVC classification errors. Must validate that tick-rule-based VPIN retains useful signal.

### 6.3 Hawkes Process Branching Ratio

**Reference**: Muhle-Karbe, Ouazzani Chahdi, Rosenbaum, Szymanski (2026). "A unified theory of order flow, market impact, and volatility." arXiv:2601.23172

With classified trades:
- Estimate separate Hawkes intensities for buy and sell arrivals
- Compute **branching ratio** n (endogeneity): what fraction of trades are reactions to other trades
- Estimate H_0 (persistence of core flow) which pins down volatility roughness and impact exponent
- Branching ratio n ~ 0.6-0.8 is typical for liquid futures (high endogeneity)

**What this unlocks**: R12 identified Hawkes branching ratio as a promising signal for regime detection but blocked on trade classification. With classified trades, we can estimate real-time endogeneity and use it as a CBS/strategy regime gate.

### 6.4 Toxic Flow and Adverse Selection Metrics

With classified trades:
- **Net buy pressure**: Fraction of volume that is buyer-initiated per interval
- **Signed trade size**: Are large trades predominantly buy or sell? (metaorder detection)
- **Adverse selection cost**: Average loss to trade initiator = E[mid_t+1 - mid_t | buy] - E[mid_t+1 - mid_t | sell]
- **Kyle's lambda**: Price impact per unit of signed volume (measures information asymmetry)

### 6.5 Trade-Level Features for Alpha

- **Buy/sell run length**: Consecutive same-direction trades (momentum proxy)
- **Direction reversal frequency**: How often does trade direction flip (noise vs signal)
- **Size-weighted direction**: Are large trades more informed?
- **Time-of-day direction bias**: Session patterns in directional flow

---

## 7. Recommended Implementation Plan for TAIFEX

### Phase 1: Core Classification Pipeline (Immediate)

**Algorithm**: EMO variant (best for large-tick futures)

```
def classify_trade(trade_price, best_bid, best_ask, prev_direction):
    """EMO algorithm adapted for large-tick futures."""
    if trade_price >= best_ask:
        return BUY
    elif trade_price <= best_bid:
        return SELL
    else:
        # Inside spread: compare to midpoint
        mid = (best_bid + best_ask) / 2
        if trade_price > mid:
            return BUY
        elif trade_price < mid:
            return SELL
        else:
            # At midpoint: tick rule fallback
            return prev_direction
```

**Data requirements** (all available):
- Trade price and volume (have)
- Concurrent best bid/ask (have, from L5 snapshots)
- Previous trade direction (maintained in state)

**Implementation location**: `src/hft_platform/normalizer.py` or new `src/hft_platform/trade_classifier.py`

**Performance**: O(1) per trade, zero allocations, compatible with hot path

### Phase 2: Validation (Week 1)

Since we lack ground-truth labels, validate using known properties:
1. **Autocorrelation test**: Signed OFI should be positively autocorrelated (persistence of order flow). If not, classification may be noise.
2. **Price impact test**: Buy-classified trades should predict positive short-term price moves. Compute E[dP | buy] vs E[dP | sell]; they should be symmetric and opposite-signed.
3. **Spread position test**: Trades classified as buys should cluster near/above the ask; sells near/below the bid. Plot distribution.
4. **Volume imbalance test**: Net signed volume should correlate with concurrent price change (Cont-Kukanov-Stoikov linear relationship).

### Phase 3: Downstream Signal Construction (Week 2-3)

1. **Signed OFI**: Replace unsigned OFI with trade-signed OFI; compare IC
2. **Hawkes estimation**: Fit bivariate Hawkes (buy, sell) arrival process; estimate branching ratio
3. **VPIN**: Compute tick-rule-based VPIN; validate against Andersen-Bondarenko critique
4. **Adverse selection**: Measure per-trade adverse selection cost by trade direction

### Phase 4: ML Enhancement (Month 2, if needed)

If Phase 2 validation shows accuracy below 85%:
1. Label "easy" trades (clearly at bid/ask) as training data
2. Train gradient boosting model using L5 book features
3. Use model for ambiguous trades only (hybrid approach)

---

## 8. Key Papers Referenced

### Foundational (Pre-2010)

| ID | Authors | Title | Key Finding |
|----|---------|-------|-------------|
| -- | Lee & Ready (1991) | Inferring Trade Direction from Intraday Data | Quote rule + tick rule hybrid; 5s delay |
| -- | Hasbrouck (1991) | Measuring the Information Content of Stock Trades | VAR model for trade impact; signed trade = key input |
| -- | Odders-White (2000) | On the Occurrence and Consequences of Inaccurate Trade Classification | LR achieves 85% on NYSE; 93% for at-bid/ask trades |
| -- | Ellis, Michaely, O'Hara (2000) | Accuracy of Trade Classification Rules: Evidence from Nasdaq | Quote, tick, LR achieve 76%, 78%, 81% on NASDAQ |
| -- | Theissen (2001) | A Test of the Accuracy of the Lee/Ready Trade Classification Algorithm | LR achieves 72.8% on Frankfurt; tick test comparable |
| -- | Boehmer, Grammig, Theissen (2007) | Estimating PIN -- Does Trade Misclassification Matter? | Misclassification biases PIN downward |
| -- | Chakrabarty, Li, Nguyen, Van Ness (2007) | Trade Classification for ECN Trades | CLNV algorithm with 30% spread zone |

### Modern Methods (2010-2020)

| ID | Authors | Title | Key Finding |
|----|---------|-------|-------------|
| arXiv:1011.6402 | Cont, Kukanov, Stoikov (2014) | The Price Impact of Order Book Events | OFI = signed flow; linear price impact; OFI needs trade classification |
| arXiv:1207.6325 | Dayri & Rosenbaum (2012) | Large Tick Assets: Implicit Spread and Optimal Tick Size | Large-tick assets have implicit spread < tick; affects classification |
| -- | Easley, Lopez de Prado, O'Hara (2012) | Bulk Classification of Trading Activity | BVC for volume-time bars; used in VPIN |
| -- | Andersen & Bondarenko (2014) | Assessing Measures of Order Flow Toxicity | **BVC inferior to tick rule; VPIN predictive power is artifact of BVC errors** |
| -- | Chakrabarty, Pascual, Shkilko (2015) | Evaluating Trade Classification Algorithms | TR=90.8%, LR=92.6%, BVC=80% on ITCH data |
| -- | Panayides, Shohfi, Smith (2019) | Bulk Volume Classification and Information Detection | BVC captures informative flow better on Euronext; rehabilitates BVC partially |

### Recent (2020+)

| ID | Authors | Title | Key Finding |
|----|---------|-------|-------------|
| SSRN:3748290 | Jurkatis (2020/2022) | Inferring Trade Directions in Fast Markets | FI algorithm: 95% accuracy, halves misclassification vs LR |
| -- | Grauer, Schuster, Uhrig-Homburg (2022) | Option Trade Classification | Standard rules 6-47% worse for options; new depth-based rules help |
| arXiv:2601.23172 | Muhle-Karbe, Ouazzani Chahdi, Rosenbaum, Szymanski (2026) | Unified Theory of Order Flow, Impact, Volatility | Signed flow -> Hawkes -> rough vol; needs trade classification |
| arXiv:2403.09267 | Kolm, Turiel, Westray (2024) | Deep Limit Order Book Forecasting | Large-tick = more predictable from L5 book; DL on LOB state |

---

## 9. Risk Assessment and Caveats

### 9.1 Known Risks

1. **No ground truth for TAIFEX**: We cannot directly measure our classification accuracy. All validation is indirect.
2. **Large-tick regime**: TMFD6 3-point spread means ~1/3 of trades may be ambiguous (inside spread or at midpoint). However, for spread = 1 tick periods, almost all trades are at bid or ask.
3. **Timestamp synchronization**: Quote and trade timestamps must be aligned. TAIFEX provides exchange timestamps, but network/processing delays could introduce misalignment.
4. **VPIN sensitivity**: Even with good classification, VPIN may not be useful for TAIFEX given the Andersen-Bondarenko critique and our R12 experience (-30.6% DD).

### 9.2 Mitigations

1. Validate classification using distributional tests (price impact symmetry, spread position distribution)
2. Start with EMO (simplest, most robust for large-tick)
3. Use classification confidence: trades clearly at bid/ask = high confidence; trades near midpoint = low confidence. Weight downstream signals by confidence.
4. Compare tick-rule-based VPIN against BVC-VPIN to confirm Andersen-Bondarenko findings on our data.

---

## 10. Conclusion and Recommendation

**PROCEED with implementation.** Trade classification is the single highest-leverage infrastructure investment we can make for microstructure alpha research.

**Priority order**:
1. **Implement EMO classifier** using L1 bid/ask (2-3 hours of work, O(1) hot-path compatible)
2. **Validate** using price impact and OFI autocorrelation tests (1 day)
3. **Compute signed OFI** and compare IC against unsigned OFI (1 day)
4. **Estimate Hawkes branching ratio** on classified trade arrivals (2-3 days)
5. **Re-evaluate VPIN** with tick-rule classification (1 day)
6. **Decision point**: If signed OFI IC > 2x unsigned OFI IC, invest in ML-enhanced classification

**Expected outcome**: Unlock 4-5 signal families (signed OFI, Hawkes, toxic flow, adverse selection, metaorder detection) that were previously blocked. This is the "new data source" that R20 concluded was needed -- it is not new data, but new INFORMATION extracted from existing data.

---

## Appendix A: TAIFEX-Specific Implementation Notes

### A.1 Data Alignment

Our tick data pipeline:
```
Exchange -> BrokerFacade -> Normalizer -> LOBEngine -> FeatureEngine
```

Trade classification should be inserted at the Normalizer stage:
- Normalizer receives both tick events (trades) and bidask events (quotes)
- At each trade event, the most recent bidask snapshot provides best_bid/best_ask
- Classification result is stored as a field on the TickEvent or as a parallel signed_volume stream

### A.2 Scaled Integer Considerations

All prices are scaled x10000. Classification comparison is straightforward:
```python
mid_x2 = best_bid + best_ask  # avoid division, compare 2*price vs mid_x2
trade_price_x2 = trade_price * 2
if trade_price_x2 > mid_x2:
    direction = BUY
elif trade_price_x2 < mid_x2:
    direction = SELL
else:
    direction = tick_rule_fallback(prev_direction)
```

This avoids any float arithmetic on the hot path, complying with the Precision Law.

### A.3 ClickHouse Storage

Add `trade_direction` column (INT8: +1=buy, -1=sell, 0=unclassified) to the tick data table. This enables historical analysis and backtesting of signed flow signals without re-computing classification.

### A.4 Confidence Weighting

For downstream signals, weight each classified trade by confidence:
- Trade at ask or above: confidence = 1.0
- Trade at bid or below: confidence = 1.0
- Trade inside spread, clearly above/below mid: confidence = 0.8
- Trade at midpoint, classified by tick rule: confidence = 0.5

Signed OFI = sum(direction_i * volume_i * confidence_i)
