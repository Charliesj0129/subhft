# Trading Electrons: Predicting DART Spread Spikes in ISO Electricity Markets

**Authors**: Emma Hubert and Dimitrios Lolas (Paris Dauphine, Princeton)
**Date**: January 2026
**Topic**: Day-Ahead vs Real-Time (DART), Spread Trading, Price Impact Modeling, Optimal Allocation

## Summary

The paper focuses on forecasting and trading "Data-Ahead vs Real-Time" (DART) price spreads, a key volatility/risk driver in wholesale electricity markets (NYISO, ISO-NE, ERCOT). It constructs a unified framework for forecasting DART spikes using logistic regression on load/congestion data and proposes a novel **"Price Impact Model"** derived from the local slope of the Day-Ahead (DA) bid stack (inverse supply/demand curve) to scale virtual trades effectively.

## Key Concepts

1.  **DART Spread**: Difference between Day-Ahead price (auction) and Real-Time price (balancing). Large deviations (spikes) are caused by grid congestion, load forecast errors, or unit contingencies.
2.  **Market Impact Model**:
    - **Idea**: Large "Virtual Trades" (speculative) shift the DA clearing price.
    - **Formula**: $I(q, t, z) = k_E^\pm \cdot S_t + k_z \cdot q_{t,z}$.
      - $k_E^\pm$: **System-Wide Energy Impact**, asymmetric for buying ($k_E^+$) vs selling ($k_E^-$). Derived from the slope of aggregate supply/demand curves.
      - $k_z$: **Zonal Congestion Impact**, local sensitivity.
    - **Implication**: Buy Impact $\neq$ Sell Impact. Selling into a steep demand cliff causes a massive price drop (e.g., $k_E^-$ is high in Winter Peak).
3.  **Optimal Scaling**: The paper derives a closed-form solution for optimal trade size $q^*_{t,z}$ that balances expected profit (Signal $x_{t,z}$) against quadratic impact costs ($k_z q^2$).

## Implications for Our Platform

- **Local Bid-Stack Impact**: We can apply this logic to crypto/LOB trading. Instead of a fixed impact model (e.g., linear/sqrt), we can measure the **instantaneous slope** of the order book (volume density) to estimate $k_{buy}$ and $k_{sell}$.
- **Asymmetric Liquidity**: Just like electricity markets, crypto order books are often thin on one side (e.g., after a crash, bid side is thin). A strategy should dynamically adjust its size based on the observed "slope" of the book.
- **Spike Prediction**: We can train a classifier (Logistic Regression or simple NN) to predict "Spread Spikes" (e.g., funding rate spikes, localized exchange deviations) using on-chain/cross-exchange data.

## Tags

#SpreadTrading #PriceImpact #MarketMicrostructure #Optimization #ElectricityMarkets #DART
