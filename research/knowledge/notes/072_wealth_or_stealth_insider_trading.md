# Wealth or Stealth? The Camouflage Effect in Insider Trading

**Authors**: Jin Ma, Weixuan Xia, Jianfeng Zhang
**Date**: 2025
**Topic**: Insider Trading, Kyle Model, Stealth Trading, Camouflage Effect, Order Flow

## Summary

The paper extends the classic Kyle (1985) model to a setting with multiple liquidity traders and legal penalties. It mathematically derives the "Camouflage Effect": Insiders optimize their trade size to blend in with the noise of the crowd.
The key finding is the existence of a **Stealth Index ($\gamma$)**:

- Insiders scale their trades such that $InsiderVolume \propto N^\gamma$, where $N$ is the number of liquidity traders.
- For the optimal stealth strategy, $\gamma$ is typically in $(0, 1/2)$. This means insiders trade _larger_ than a single noise trader but _smaller_ than the aggregate crowd, ensuring their price impact asymptotically vanishes as $N \to \infty$.

## Key Concepts

1.  **Stealth Index ($\gamma$)**:
    - $\gamma = 0$: Insider trades like a single retail trader (too small, leaves money on the table).
    - $\gamma = 1/2$: Insider trades proportional to total market volume (too aggressive, gets detected).
    - **Optimal $\gamma \approx 0.3-0.4$**: Insiders clustering in "medium" trade sizes.
2.  **Detection Mechanics**:
    - Regulators look for _abnormal_ order flow imbalances.
    - Large $N$ (liquidity pool) raises the threshold for detection, allowing insiders to trade more in absolute terms but less in percentage terms.
3.  **Camouflage**:
    - Insiders rely on the _number_ of noise traders, not just total volume. High participation counts matter more than just high volume from a few whales.

## Implications for Our Platform

- **Detection of Informed Flow**:
  - **Action**: When analyzing order flow for "Informed" signals, look for **Medium-Sized** trades that persist, rather than huge block trades (which are often liquidity motivated) or small retail trades.
  - **Cluster Analysis**: Focus on trade sizes that are "anomalously consistent" in the 30th-70th percentile of trade size distribution, rather than the top 1%.
- **Execution Strategy**:
  - If we are trading on alpha, we should mimic this behavior. Don't be the largest print. Scale our execution size to be $N^{0.4}$ of the crowd count.

## Tags

#InsiderTrading #KyleModel #StealthTrading #OrderFlow #MarketMicrostructure
