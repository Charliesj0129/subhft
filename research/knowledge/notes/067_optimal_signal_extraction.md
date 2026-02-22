# Optimal Signal Extraction from Order Flow: A Matched Filter Perspective

**Authors**: Sungwoo Kang
**Date**: 2025
**Topic**: Signal Extraction, Order Flow Normalization, Matched Filter

## Summary

This paper argues that the traditional practice of normalizing order flow by **Trading Volume ($V_i$)** is fundamentally flawed for extracting informed trading signals. Instead, order flow should be normalized by **Market Capitalization ($M_i$)**. The authors demonstrate theoretically and empirically (using Korean market data) that informed traders scale their positions relative to firm size ($M_i$), while noise traders respond to daily liquidity ($V_i$). Normalizing by volume introduces heteroskedastic noise proportional to inverse turnover.

## Key Concepts

1.  **Matched Filter Theory**:
    - In signal processing, maximizing SNR requires weighting the signal by its expected structure.
    - Since informed order flow scales with Market Cap ($Q_{inf} \propto \alpha_i \cdot M_i$), the optimal normalization is dividing by $M_i$.
2.  **Heteroskedastic Noise**:
    - Volume normalization ($D_i / V_i$) effectively scales the signal by inverse turnover ($M_i / V_i$).
    - Since turnover varies widely, this corrupts the signal for stocks with high/low turnover.
3.  **Empirical Evidence**:
    - Market Cap normalization achieves **1.32-1.92x higher correlation** with future returns than Volume normalization.
    - It remains robust across signal strength and noise levels.

## Implications for Our Platform

- **Alpha Signal Construction**:
  - **CRITICAL ACTION**: Review all our alpha signals that use "Order Flow / Volume".
  - **Change**: Switch to "Order Flow / Market Cap" or "Order Flow / Average Daily Value Traded (smoothed)" if Market Cap data is unavailable (though Market Cap is preferred).
  - **Backtest**: Retest our strategies with this new normalization. Expect significant improvement in Sharpe.
- **Feature Engineering**:
  - When building ML models, use `OrderFlow / MarketCap` as a feature, not just the standard `OrderFlow / Volume`.

## Tags

#SignalProcessing #OrderFlow #AlphaGeneration #MarketMicrostructure #Normalization
