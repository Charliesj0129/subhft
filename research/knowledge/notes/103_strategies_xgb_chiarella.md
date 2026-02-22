# Understanding Intra-day Price Formation Process by Agent-Based Financial Market Simulation: Calibrating the Extended Chiarella Model

**Authors**: Kang Gao, Perukrishnen Vytelingum, Stephen Weston, Wayne Luk, Ce Guo
**Date**: 2022-08
**Topic**: Agent-Based Simulation (ABM), Price Formation, Surrogate Modeling, XGBoost, Extended Chiarella Model

## Summary

The paper presents **XGB-Chiarella**, a framework for calibrating Agent-Based Models (ABMs) to realistic intra-day financial data using **Machine Learning Surrogate Modeling** (XGBoost).

- **The Model (Extended Chiarella)**:
  - 3 Agent Types: **Fundamentalists** (Mean Reversion), **Momentum** (Trend Following), **Noise Traders** (Random Walk).
  - Price Impact: Linear Kyle's Lambda ($P_{t+\Delta} - P_t = \lambda D$).
  - **Fundamental Value**: Extracted via **Kalman Smoother** on historical prices (innovative step to get "ground truth" $V_t$).
- **Calibration (The Hard Part)**:
  - Goal: Find parameters $(\kappa, \beta, \sigma_N)$ that minimize the distance between Simulated and Real **Stylized Facts**.
  - **Stylized Facts Distance**: $D = KS_{Ret} + \Delta Vol + \Delta ACF_{Ret} + \Delta ACF_{Vol}$.
  - **Surrogate Model**: Since running ABM is slow, train an **XGBoost Regressor** to predict the Distance $D$ from parameters $\theta$. Use Bayesian Optimization to finding global minimum of the surrogate, then verify with real ABM.
- **Results**:
  - Calibrated on 75 stocks (Nasdaq, LSE, HKEX).
  - Reproduces Fat Tails, Volatility Clustering, and minimal Return Autocorrelation.
  - Proves a **Universal Price Formation Mechanism**: The same 3-agent model fits diverse markets, just with different parameter values.

## Key Concepts

1.  **Surrogate Modeling for ABM**:
    - Simulating markets is computationally expensive.
    - Solution: Run a small batch of simulations $\to$ Train an ML model (XGBoost) to mapping Parameters $\to$ Error Metrics $\to$ Optimize the ML model $\to$ Verify.
    - This "Simulation-Based Inference" is crucial for realistic digital twins.
2.  **Kalman Smoother for Fundamental Value**:
    - Instead of assuming $V_t$ is constant or a geometric Brownian motion, they treat $V_t$ as a hidden variable in observed prices $P_t$ and extract it via Kalman Smoothing. This gives Fundamentalist agents a realistic target.

## Implications for Our Platform

- **Simulation Tuning**:
  - We can use the **XGBoost Surrogate** technique to tune our own HFT simulator. If we want our simulator's "Market Impact" to match real L1/L2 data, we define a distance metric (e.g., LOB depth distribution error) and use XGBoost to find the best `lambda` and `kappa` for our agents.
- **Fundamental Signal**:
  - Implement **Kalman Smoothing** on mid-prices to generate a "Fair Value" signal. This can be used as a feature for our RL agent (Feature: `MidPrice - KalmanValue`).

## Tags

#ABM #Simulation #Calibration #XGBoost #PriceFormation #MarketMicrostructure #KalmanFilter
