# DeePM: Regime-Robust Deep Learning for Systematic Macro Portfolio Management

**Authors**: Kieran Wood, Stephen J. Roberts, Stefan Zohren
**Date**: 2026
**Topic**: Deep Learning, Macro Investing, Portfolio Optimization, Robustness

## Summary

The paper introduces **DeePM** (Deep Portfolio Manager), an end-to-end deep learning framework for systematic macro trading.

- **Architecture**:
  - **Temporal**: Hybrid VSN + LSTM + Attention to process per-asset time series.
  - **Cross-Sectional**: "Directed Delay" attention to handle asynchronous global closes (e.g., Tokyo vs NY) without look-ahead bias.
  - **Structural**: A **Macro Graph Prior** (GNN) that regularizes the model using economic knowledge (e.g., Oil $\to$ Inflation $\to$ Rates).
- **Objective**:
  - Optimizes a **SoftMin Sharpe** ratio, which acts as a proxy for **Entropic Value-at-Risk (EVaR)**. This trains the model to be robust against "Worst-Case" historical windows, not just average performance.
- **Results**:
  - Outperforms "Momentum Transformer" by ~50% in risk-adjusted returns.
  - Navigates "CTA Winter" and post-2020 volatility well.

## Key Concepts

1.  **Directed Delay (Causal Sieve)**:
    - Problem: Global markets close at different times. Mixing "Close" prices directly creates look-ahead.
    - Solution: Enforce a strictly lagged attention mask so the model only attends to information available _at decision time_.
2.  **Macro Graph Prior**:
    - Instead of letting the model learn all correlations from noisy data, inject a "Prior Graph" of known economic links (Equity-Bond correlation, Commodity-FX links).
    - Uses a GAT (Graph Attention Network) to refine reliable signals.
3.  **SoftMin / Differentiable EVaR**:
    - Training on average Sharpe leads to overfitting "easy" years.
    - Training on SoftMin Sharpe forces the model to maximize performance on the _hardest_ windows (Minimax).

## Implications for Our Platform

- **Portfolio Optimizer**:
  - **Action**: Replace our standard Mean-Variance optimizer with a **SoftMin Sharpe** objective in the RL agent's reward function. This will make our agents more robust to drawdowns.
- **Graph Neural Networks**:
  - **Alpha**: We can build a `MacroGraphAlpha` that uses a GAT to propagate shocks across our assets (e.g., if BTC crashes, how does it affect ETH and COIN?).
- **Asynchronous Data**:
  - **Data Check**: Review our current backtester to ensure we aren't leaking future data when trading assets with different closing times (e.g., Crypto 24/7 vs Stocks 9-4).

## Tags

#DeepLearning #PortfolioManagement #MacroInvesting #GraphNeuralNetworks #RobustOptimization #EVaR
