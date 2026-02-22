# Optimal Signal Extraction from Order Flow: A Matched Filter Perspective on Normalization and Market Microstructure

**Authors**: Sungwoo Kang
**Date**: 2025-12
**Topic**: Signal Processing, Order Flow Normalization, Market Microstructure

## Summary

The paper argues that when extracting Alpha from order flow, one should **normalize by Market Cap** ($S_{MC} = D_i / M_i$) rather than the traditional **Trading Volume** ($S_{TV} = D_i / V_i$).

- **Theoretical Argument**:
  - **Informed Traders** (Institutional) scale positions by **Market Cap** (capacity/risk limits). $Q_{inf} \propto \alpha \cdot M_i$.
  - **Noise Traders** (Retail) scale positions by **Daily Volume** (attention/liquidity). $Q_{noise} \propto V_i$.
  - **Matched Filter**: To recover $\alpha$, dividing by $M_i$ yields $\alpha + \text{noise}$, whereas dividing by $V_i$ yields $\alpha \cdot (M_i/V_i) + \text{noise}$, introducing heteroskedastic noise from turnover $(V_i/M_i)$.
- **Empirical Results**:
  - Tested on 2.1 million stock-day observations in Korea.
  - Cap-normalized flow ($S_{MC}$) has **1.32x higher correlation** with future returns than volume-normalized flow ($S_{TV}$).
  - In a horse race regression, $S_{MC}$ is highly significant ($t=10.99$), while $S_{TV}$ becomes negative ($t=-6.81$), suggesting volume normalization captures spurious noise.

## Key Concepts

1.  **Matched Filter**:
    - In signal processing, you multiply the received signal by the "template" of the source. The source (informed trader) uses $M_i$ as a template.
2.  **Turnover as Noise**:
    - High turnover ($V_i/M_i$) is often a proxy for investor disagreement/confusion. Normalizing by Volume implicitly down-weights signals from low-turnover stocks (often stable, informed names) and up-weights high-turnover noise.

## Implications for Our Platform

- **Alpha Factor Construction**:
  - **Action**: Review all our "Flow" alphas (e.g., Order Flow Imbalance, Net Buying Pressure).
  - **Change**: Construct a version normalized by **Shares Outstanding** or **Market Cap**, instead of just Volume or Count.
  - **Test**: Run a backtest comparing `OFI / Volume` vs `OFI / MarketCap`.
- **Institutional Tracking**:
  - ** Insight**: If we want to track "Smart Money", assumption is they care about % of Company Owned. Retail cares about % of Daily Volume Traded.

## Tags

#SignalProcessing #OrderFlow #Normalization #MarketMicrostructure #AlphaFactors
