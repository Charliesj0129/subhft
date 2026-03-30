# Deep-Dive: Michael, Cucuringu & Howison (2022) — Option Volume Imbalance

**Paper**: "Option Volume Imbalance as a predictor for equity market returns" (arXiv:2201.09319v1)
**Data**: NASDAQ PHLX (PHOTO) and NOM (NOTO) exchanges, 2015-01-02 to 2019-12-31, 10-minute aggregate buckets
**Universe**: US equities with listed options (~750-1000 per day) + 11 ETFs

---

## 1. Exact Methodology: OVI Definition

### Formula

OVI is defined for asset i, day d, market participant class m as:

```
OVI[i,d,m] = (sum_j V_Up[i,j,d,m] - sum_j V_Down[i,j,d,m]) / (sum_j V_Up[i,j,d,m] + sum_j V_Down[i,j,d,m])
```

Where j indexes option contracts, and:
- **V_Up** (positive-view volume) = Call-Buy volume + Put-Sell volume
- **V_Down** (negative-view volume) = Call-Sell volume + Put-Buy volume

OVI lies in [-1, +1]. When total volume = 0, OVI = 0 by convention.

### Three OVI Variants
1. **Volume-based OVI**: X = V (raw contract volume). Primary metric used.
2. **Trade-based OVI**: X = T (number of transactions, ignoring size). Worst performer.
3. **Nominal-volume OVI**: X = P_option * V (price-weighted volume). Prevents cheap OTM options from dominating.

### Aggregation Window
- **Daily aggregation** for the main results. All option trades for the day are summed before computing OVI.
- Data arrives in **10-minute buckets** (39 per day, from 9:40 to 16:00 ET), but main analysis uses end-of-day totals.
- Intraday OVI ("open-t" OVI) was explored in Appendix E — using cumulative volume from market open up to time t.

### Signal Usage
The **sign** of OVI is used as a directional predictor for next-day returns. Positive OVI -> buy the underlying; negative OVI -> sell. Bet sizes can be uniform or weighted by OVI magnitude, volume, or IV.

---

## 2. Market-Maker vs Non-Market-Maker Classification

### How They Separate MPCs
The NASDAQ data natively provides volumes decomposed into **five Market Participant Classes (MPCs)**:
1. **Firm** (proprietary trades)
2. **Broker**
3. **Market Maker**
4. **Customer** (ordinary retail/institutional)
5. **Professional Customer**

This classification comes directly from NASDAQ's trade reporting — it is an **exchange-level regulatory classification**, not something the authors construct.

### Double-Counting
Each transaction is counted from **both sides**. If a Customer buys a call from a Market Maker, it counts as Customer-Buy AND Market Maker-Sell. This is intentional — the same trade generates opposite OVI contributions for different MPCs.

### Why Market Maker OVI Is the Strongest Signal
Market Makers do not trade directionally — they profit from spreads. When they accumulate a large imbalanced position (e.g., net short calls), it reflects the aggregate directional intent of all counterparties. The Market Maker OVI is essentially a **mirror image** of informed flow, inverted.

- Customer OVI correlation with Market Maker OVI: **-0.6** (strongly negative)
- Market Maker OVI SR: **3.5-4.5** (annualized, depending on quantile group)
- Customer OVI SR: **2.7-3.5** (same sign direction — both profitable because MMs are on the other side of informed trades)

### CRITICAL: Can We Replicate on TAIFEX?

**Almost certainly NO for the MPC decomposition.** TAIFEX/TXO data does not natively classify trades by market participant type. We would get **aggregate volumes only** — all participants combined.

However, the paper also shows that the **aggregate OVI** (all MPCs combined) is still a significant predictor, just weaker than the MPC-decomposed version. The aggregate OVI is what we would compute from TXO data.

**Possible workaround**: Use trade-size buckets as a proxy for participant type:
- Very large lots -> likely institutional/professional
- Small lots -> likely retail
- This is crude but common in markets without MPC tagging.

---

## 3. Signal Construction Step-by-Step

From raw option trades to final signal:

1. **Collect** all option transactions for the day, for each underlying asset
2. **Classify** each transaction by direction: Buy or Sell
3. **Map** to positive/negative view:
   - Call-Buy -> V_Up (positive view)
   - Call-Sell -> V_Down (negative view)
   - Put-Buy -> V_Down (negative view)
   - Put-Sell -> V_Up (positive view)
4. **Sum** V_Up and V_Down across all option contracts j for that underlying
5. **Normalize**: OVI = (V_Up - V_Down) / (V_Up + V_Down)
6. **Signal**: s_i,d = OVI_i,d (the raw OVI value)
7. **Direction**: sign(s_i,d) determines long (+1) or short (-1) position in the underlying
8. **Bet size**: Uniform (equal $1 per asset) or weighted by |OVI|, volume, or IV
9. **Trade**: Enter at close of day d, exit at open of day d+1 (overnight return)
10. **P&L**: sum over all assets of b_i,d * excess_return_i,d * sign(s_i,d)

### Key Detail: Buy/Sell Classification
The paper uses exchange-reported buy/sell labels. In our TXO data, we would need to infer trade direction using the **Lee-Ready algorithm** (compare trade price to bid-ask midpoint) or use exchange-reported aggressor side if available.

---

## 4. Statistical Results

### Sharpe Ratios (Annualized, Uniform Weighting, PHOTO dataset)

| MPC | Q1 (all) | Q3 (top 60%) | Q5 (top 20%) |
|---|---|---|---|
| **Market Maker** | **3.5** | **4.4** | **3.8** |
| Customer | 2.7 | 3.5 | 3.0 |
| Broker | 2.1 | 2.5 | 2.3 |
| Professional Customer | -1.0 | -1.4 | -1.1 |
| Firm | ~0 | ~0 | ~0 |

- p-values for Market Maker, Customer, Broker SRs: **0.00** (up to 2 decimal places) across all quantile ranks
- Firm and Professional Customer: p-values ~0.5 (not significant)
- NOTO exchange (second dataset): Market Maker SR ~2.5-3.0 (weaker but consistent)

### Cumulative Returns
- Market Maker OVI: **17-25% cumulative return** over 5 years (2015-2019)
- Customer OVI: clear upward trend, slightly lower
- PPD (Profit Per Dollar): up to **4 basis points per day** for tail portfolios with strongest signals

### Return Horizons Tested

| Return Type | Market Maker SR | Significance |
|---|---|---|
| **Overnight (CL->OP)** | **3.5-4.5** | **Strongest** |
| Close-to-Close (CL->CL) | ~2.5-3.5 | Significant |
| Intraday (OP->CL next day) | ~0.5-1.0 | Weak |

**The overnight return is BY FAR the strongest signal.** Close-to-close is significant but most of its predictability stems from the overnight component.

### Intraday Evidence (Appendix E)
- Tested "open-t" OVI for {1, 5, 10, 30, 60, 90, 180}-minute future returns
- Market Makers: **moderately high SR at midday** (maximized at t=13:10) for 30-180 minute returns
- "Holding period from midday to opening of next day seems optimal"
- BUT: After full Bonferroni correction across all tests (3 QR x 10 return modes x 5 MPCs x 39 time points), **no intraday SR survives multiple-testing correction**
- Authors call these "promising observations" that need more data to confirm
- **Bottom line**: Intraday evidence is suggestive but NOT statistically proven in this paper

### R-squared / IC
- The paper **explicitly avoids** R-squared and IC metrics, stating: "a predictor that is successful in capturing the signs of future returns, may be deemed uninformative with respect to euclidean distance" and "while a predictor's sign may match the observations perfectly, we may still observe a small corresponding R^2 value"
- They use a custom **P&L regression** that maximizes cumulative P&L directly rather than minimizing squared error
- OLS regression R^2 values for OVI vs returns are described as "quite low" — they argue this is misleading for directional predictions
- **No IC numbers are reported.** The metric they use is SR and PPD.

---

## 5. High-IV Options Finding

### Definition
Options are sorted into **4 quartile buckets** by implied volatility (calculated via Black-Scholes without dividends):
- Bucket 1: lowest 25% of IV values
- Bucket 4: highest 25% of IV values

### Results
- **Bucket 4 (highest IV) is the best performer across ALL MPCs** (Brokers, Market Makers, Customers)
- Bucket 4 significantly outperforms all other buckets except Bucket 3 for Brokers
- Bucket 4 outperforms the complete (unfiltered) dataset in PPD for Brokers and Market Makers, and in SR for Brokers
- Bucket 3 is consistently the second best
- **"Most of the predictability stems from high implied volatility contracts"** (direct quote)

### Interpretation
High-IV options correspond to:
- Options on volatile or event-driven underlyings
- Options during periods of uncertainty
- Situations where informed traders have the most edge and are most likely to trade directionally

This is NOT about moneyness filtering — it's about the **implied volatility level** of the option contract itself. An ATM option on a volatile stock has higher IV than an ATM option on a utility stock.

### What This Means for TXO
- We should weight or filter TXO volumes by the IV of the traded option
- Higher-IV options (further OTM or during volatile periods) carry more signal
- This is computable from TXO data if we have strike, expiry, and traded price

---

## 6. Put vs Call Asymmetry

### Finding
**Put options are more informative than call options**, consistently across all MPCs.

### Evidence
- Options are bucketed by **Delta** into 4 quartiles:
  - Bucket 1: most negative delta (deep ITM puts)
  - Bucket 4: most positive delta (deep ITM calls)
- **Bucket 1 (ITM puts) outperforms all other buckets** for Market Makers and Customers (statistically significant)
- Also confirmed via **Rho** bucketing: first two Rho buckets (put options) are the best performing, statistically significant for all MPCs
- The authors state: "put options play a more important role in predicting future returns compared to call options"

### Magnitude
- The paper shows this via pairwise SR difference tests with significance codes
- The difference between put-heavy and call-heavy portfolios is significant at the 0.001 level (***) for Market Makers

### Consistency
- Consistent across both PHOTO and NOTO datasets
- Consistent across different time periods within 2015-2019
- Aligns with prior literature: puts are more often used by informed traders anticipating bad news (Pan & Poteshman 2006)

### Implication for TXO
- Weight put volume more heavily than call volume in our OVI construction
- Or compute separate put-OVI and call-OVI and combine with higher weight on puts

---

## 7. Transaction Cost Model

### The Paper's Approach to Costs
**They do NOT model transaction costs.** Direct quote from the paper:

> "nor, given the existence of transaction costs (which we do not take into account here), does it show the realistic return obtained from the strategy. Instead, this metric is a measure of predictability across a number of stocks based on the predictor s."

The P&L metric is a **hypothetical measure of predictability**, not a tradeable strategy return.

### What We Can Infer
- PPD of up to **4 bps/day** for the strongest quantile groups
- If we assume US equity round-trip costs of ~2-5 bps (institutional) or ~10-20 bps (retail with spreads):
  - At 4 bps/day, the strategy is marginal for institutional and unprofitable for retail on US equities
  - BUT: they trade ~750 stocks per day, so the per-stock position size is tiny — more like a cross-sectional factor than a concentrated bet

### Implications for TMFD6
- Our cost is 1.33 bps RT (4 pts on ~300K NTD contract value)
- If the TXO OVI signal generates >1.33 bps of directional edge per trade on TMFD6, it's profitable
- With overnight holding, TMFD6 typically moves 50-200 pts (~17-67 bps), so even a small directional edge would cover costs
- **Key risk**: their 4 bps PPD is across ~750 diversified stocks. On a single futures contract, the signal-to-noise ratio is much worse.

---

## 8. Transferability Assessment

### US-Equity-Specific Aspects (HARDER to transfer)

1. **MPC decomposition**: Requires exchange-level trade classification by participant type. TAIFEX almost certainly doesn't provide this. **This is the single biggest limitation.**

2. **Cross-sectional diversification**: Their P&L is averaged across ~750 stocks per day. We have ONE futures contract. The SR will be much lower for a single instrument. Their SR of 4.5 across 750 stocks would likely translate to SR << 1 for a single stock.

3. **Market-excess returns**: They strip out SPY returns. Our signal would need to be tested on raw TMFD6 returns (no equivalent benchmark to subtract).

4. **Overnight gap dynamics**: US overnight gaps reflect after-hours trading, earnings, global markets. Taiwan's overnight gap reflects US session + global developments. The mechanism may differ.

5. **Put-call parity arbitrage**: US market makers actively trade put-call parity. TXO market microstructure may differ significantly.

### Generalizable Aspects (EASIER to transfer)

1. **OVI formula itself**: Pure arithmetic on option volumes. Fully portable.

2. **Directional signal from option flow**: The economic intuition (informed traders use options for leverage) is universal and well-documented across markets.

3. **High-IV filtering**: Implied volatility computation is standard (BS model). We can compute IV from TXO traded prices.

4. **Put > Call asymmetry**: Bad-news trading via puts is a universal phenomenon, not US-specific.

5. **Volume-based OVI > Trade-based OVI**: This is a property of the signal, not the market.

6. **Non-linear signal usage**: Using sign(OVI) rather than OVI as a linear predictor is market-agnostic.

### Bottom Line Transferability Score: **MODERATE**

The core signal (option volume imbalance predicts underlying direction) should transfer. But:
- Without MPC decomposition, we lose the strongest signal (Market Maker OVI)
- With only one futures contract, we lose cross-sectional diversification
- The proven horizon is overnight, which exposes us to gap risk on a single instrument
- Intraday evidence is suggestive but NOT proven even in the original paper
- We should expect SR << 4.5 — realistically SR 0.5-1.5 if the signal transfers at all

---

## Summary of Key Numbers

| Metric | Value |
|---|---|
| Best MPC | Market Maker |
| Best SR (annualized) | 4.4 (Q3, Market Maker, overnight EMR) |
| SR p-value (MM) | 0.00 across all quantile ranks |
| Best PPD | ~4 bps/day (tail quantiles) |
| Cumulative return | 17-25% over 5 years |
| Best return horizon | Overnight (CL->OP) |
| Intraday SR | Suggestive but NOT significant after Bonferroni correction |
| Best option feature | High implied volatility (Bucket 4 >> others) |
| Put vs Call | Put volumes significantly more informative |
| Transaction costs modeled | NO — pure predictability metric |
| R-squared / IC | Not reported (paper argues R^2 is misleading for directional signals) |
| Holding period persistence | MM OVI: significant through day 2; no reversal through day 10 |
| Cross-exchange consistency | NOTO confirms PHOTO results, slightly weaker (SR ~2.5-3) |

---

## Red Flags for Our Application

1. **No intraday statistical significance** — the authors themselves couldn't prove intraday works, and they had much more data than we do
2. **No transaction costs** — the 4 bps PPD might vanish entirely under realistic costs
3. **Cross-sectional diversification** — their SR is for a portfolio of ~750 stocks; single-instrument SR will be dramatically lower
4. **MPC decomposition unavailable** — we lose the strongest signal channel
5. **Overnight holding on single futures** — high variance, gap risk, margin risk
6. **2015-2019 sample period** — pre-COVID only, no evidence of robustness through regime changes
7. **Buy/sell classification** — we need Lee-Ready or exchange aggressor labels for TXO, which we may not have
