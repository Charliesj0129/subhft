# Sources and Nonlinearity of High Volume Return Premium: An Empirical Study

**Authors**: Sungwoo Kang
**Date**: 2025
**Topic**: High Volume Return Premium (HVRP), Investor Heterogeneity, Market Microstructure

## Summary

The paper resolves a puzzle in the Korean stock market (a Low Volume Return Premium, LVRP) by showing that the "High Volume Return Premium" (HVRP) indeed exists but only for **Institutional Investors** and only when **volume is normalized by Market Cap**. Retail volume is largely "noise" or short-lived, while institutional high-conviction buying (volume/MarketCap) predicts significant positive returns (+12.12% over 50 days).

## Key Concepts

1.  **Normalization Matters (Again)**:
    - **Volume / Market Cap** = "Conviction" (Position size relative to firm value). This shows a monotonic relationship with returns.
    - **Volume / Traded Value** = "Participation" (Activity relative to daily flow). Shows no consistent signal or even negative correlation.
2.  **Investor Heterogeneity**:
    - **Institutional/Foreign**: Information-driven. Buying predicts price rise.
    - **Retail**: Attention-driven/Noise. Buying predicts short-term momentum then reversion or zero alpha.
3.  **Regime Switching**:
    - During COVID/crises, retail briefly acted as liquidity providers (contrarian), altering the dynamic.

## Implications for Our Platform

- **Signal Construction**:
  - **Action**: Separate order flow into "Retail" (small size, off-exchange/retail broker codes if available) and "Institutional" (large blocks, specific Exchange Members).
  - **Normalization**: Always normalize the "Signal" (Order Flow) by **Market Capitalization** to measure conviction.
  - **Filter**: Ignore retail-heavy flow signals for alpha, or use as a mean-reversion indicator.

## Tags

#HighVolumeReturnPremium #OrderFlow #InvestorTypes #MarketMicrostructure #Alpha
