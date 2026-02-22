# A Hidden Markov Model for Statistical Arbitrage in International Crude Oil Futures Markets

**Authors**: Viviana Fanelli, Claudio Fontana, Francesco Rotondi
**Date**: 2024-09
**Topic**: Statistical Arbitrage, Pairs Trading, Cointegration, Hidden Markov Model (HMM), Crude Oil Futures (Brent, WTI, Shanghai)

## Summary
The paper investigates statistical arbitrage strategies using a **cointegration approach** on three crude oil futures: **Brent**, **WTI**, and the recently introduced **Shanghai (INE)** crude oil futures.
*   **Methodology**:
    *   **Cointegration**: The authors prove that the three time series are cointegrated, forming a stationary *Spread* $S_t = \lambda_0 + \lambda_B F^B_t + \lambda_S F^S_t + \lambda_W F^W_t$.
    *   **Regime Switching Model**: The spread is modeled as a Mean-Reverting process with parameters modulated by a **Hidden Markov Chain** (OU-HMM):
        $$dS_t = a(X_t)(\beta(X_t) - S_t)dt + \xi(X_t)dW_t$$
    *   **Online Filtering**: An **EM Algorithm** is used to dynamically estimate the hidden state $X_t$ (Regime) and parameters (Mean Reversion Speed, Long-Term Mean, Volatility) in real-time.
*   **Strategies**:
    *   **Plain Vanilla**: Trade whenever $S_t \neq 0$.
    *   **Probability Interval (Bollinger)**: Trade when $S_t$ outside historical bands.
    *   **Prediction Interval (PredI)**: Trade when $S_t$ is outside the *Predicted* confidence interval from the HMM filter. This forward-looking strategy outperformed others.
*   **Key Findings**:
    *   **Shanghai Alpha**: Strategies including the **Shanghai** futures were significantly more profitable than traditional Brent-WTI pairs. The Shanghai contract has a higher speed of mean revision, offering more arb opportunities.
    *   **HMM Superiority**: the `PredI` strategy (using HMM forecasts) generated higher Sharpe Ratios than static Bollinger Bands, even after accounting for conservative transaction costs (80bps).

## Key Concepts
1.  **OU-HMM**:
    *   Ornstein-Uhlenbeck process where parameters depend on a hidden state. Captures "Calm" vs "Volatile" vs "Trending" regimes in the spread.
2.  **Filter-Based EM**:
    *   Recursive algorithm to update parameters $\hat{\theta}_t$ as new data arrives. Crucial for live trading where regimes shift.

## Implications for Our Platform
-   **New Arb Leg**:
    *   Add **Shanghai Crude (INE)** to our energy arb strategies. It adds significant diversification and alpha compared to just trading Brent/WTI.
-   **Dynamic Thresholds**:
    *   Instead of fixed Bollinger Bands (e.g., 2.0 sigma), use an **HMM Filter** to set dynamic entry/exit bands. If the market enters a high-volatility regime, the bands naturally widen, preventing bad entries.
-   **Implementation**:
    *   The recursive EM equations (Eq 2.7, 2.8) are computationally efficient and can be implemented in our C++ or Python strategy layer for real-time updates.

## Tags
#StatisticalArbitrage #Cointegration #HMM #CrudeOil #Shanghaiine #PairsTrading #MeanReversion
