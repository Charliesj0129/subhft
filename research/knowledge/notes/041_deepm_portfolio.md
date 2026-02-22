# DeePM: Regime-Robust Deep Learning for Systematic Macro Portfolio Management

**Authors**: Kieran Wood et al. (Oxford-Man Inst., University of Oxford)
**Date**: January 2026
**Topic**: Deep Portfolio Optimization, Regime Shifts, Robustness, Graph Neural Networks

## Summary

DeePM is a structured deep-learning architecture for systematic macro portfolio management. It addresses key challenges in financial learning: asynchronous market hours (Look-ahead bias), overfitting to high-signal regimes (Error Maximization), and poor out-of-sample robustness (Regime Fragility).

## Key Concepts

1.  **Directed Delay (Causal Sieve)**:
    - **Problem**: Global markets (Asia, Europe, US) have overlapping but distinct trading hours. Using "Close-to-Close" correlations naively can leak future information (e.g. US Close at $t$ depends on Asia Close at $t+1$).
    - **Solution**: Enforce a strict **Direct Delay** ($t-1$) for cross-sectional attention. The model can only attend to _past_ cross-sectional information, ensuring causal validity. This is termed "Causal Sieve".
2.  **Macroeconomic Graph Prior**:
    - **Problem**: Pure attention (learning all-to-all correlations) overfits noise.
    - **Solution**: Inject a **Structural Graph** (GNN) based on economic first principles (e.g., Oil $\leftrightarrow$ Energy Stocks, Rates $\leftrightarrow$ FX). This acts as a regularizer, forcing the model to respect plausible economic linkages.
3.  **Robust Objective (SoftMin)**:
    - **Problem**: Maximizing Average Sharpe Ratio ignores tail risks and encourages "lucky" strategy fitting.
    - **Solution**: Minimize **Entropic Value-at-Risk (EVaR)** via a differentiable **SoftMin** penalty on rolling window Sharpe ratios. This forces the model to perform well in the _worst_ historical periods (minimax), improving robustness to new regimes.

## Implications for Our Platform

- **Look-Ahead Prevention**: Strict adherence to timestamp logic is crucial when dealing with 24/7 crypto markets vs traditional 9-5 markets in a unified portfolio.
- **Graph Regularization**: We can build a **Crypto Sector Graph** (e.g., L1 -> DeFi -> Gaming) to guide our attention mechanism. Token correlations often follow sector narratives.
- **Robust Loss Function**: Replacing standard PPO/DQN loss with a **Robust SoftMin Loss** (penalizing worst-case trajectory return) could significantly improve live trading stability.
  - `Loss = -SoftMin(Window_Returns, temperature)`

## Tags

#DeepLearning #PortfolioManagement #RobustOptimization #GraphNeuralNetworks #SystematicMacro #RegimeSwitching
