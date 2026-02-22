# Attention Factors for Statistical Arbitrage

**Authors**: Elliot L. Epstein, Rose Wang, Jaewon Choi, Markus Pelger (Stanford University, Hanwha Life)
**Date**: 2025-11
**Topic**: Statistical Arbitrage, Attention Mechanisms, Latent Factors, Trading Costs, End-to-End Learning

## Summary

The paper proposes an **"Attention Factor Model"** that jointly learns:

1.  **Arbitrage Factors**: Latent factors constructed from firm characteristics using an Attention Mechanism ($Q_{factors} \cdot K_{stocks}^T$).
2.  **Trading Policy**: A Convolutional Network ("LongConv") that reads the _residuals_ of these factors and outputs portfolio weights.
3.  **Objective**: Maximize **Net Sharpe Ratio** (after transaction costs), not just explained variance.

- **Problem**:
  - Traditional "Two-Step" approaches first find factors (PCA) to explain variance, _then_ trade the residuals (OU process).
  - PCA factors are often "expensive" to trade (high turnover, difficult shorts).
  - Separating factor extraction from trading leads to suboptimal net returns.
- **Methodology**:
  - **Attention Factors**: Firm characteristics $X_t$ are embedded. A set of "Query Vectors" $Q$ (representing the $K$ factors) attends to these embeddings to determine factor weights $\omega_{F}$.
  - **Residual Trading**:
    - Calculate Residuals: $\epsilon_t = R_t - \beta_{t-1}^T F_t$.
    - Signal Extraction: Feed past residuals $\epsilon_{t-s:t}$ into a **LongConv** layer (1D CNN) to predict optimal portfolio weights.
  - **Loss Function**: Maximize (Net Sharpe Ratio) + $\lambda$ (Explained Variance). This ensures factors are economically meaningful _and_ profitable.
- **Key Findings**:
  - Achieves **Net Sharpe Ratio of 2.3** (after costs) vs 1.5 for PCA-based methods.
  - **"Weak Factors" Matter**: Increasing $K$ (number of factors) from 8 to 30 improved performance significantly. The model finds subtle, local correlation structures that PCA misses but that are crucial for hedging/arbitrage.
  - **Price-Based features drive performance**: Removing past return features (momentum/reversal) hurt performance more than removing fundamental features (P/E, Size).

## Key Concepts

1.  **End-to-End Arbitrage**:
    - Don't just find factors that "explain risk". Find factors that leave behind **tradable, mean-reverting residuals**.
    - The "Attention" mechanism allows the factors to dynamically rotate based on firm characteristics (e.g., "The Value Factor" might load on different stocks today vs. next month).
2.  **Weak Factors**:
    - Standard PCA keeps the top $K$ strong factors. This paper shows that for _Arbitrage_, the "Weak" factors (low explained variance) are often the best for hedging out idiosyncratic noise, isolating pure alpha.

## Implications for Our Platform

- **Architecture Upgrade**:
  - Replace standard PCA/Risk Models with a **Differentiable Factor Model**.
  - Implement an "Attention Factor" layer where $K$ latent query vectors attend to stock characteristics.
- **Loss Function**:
  - Train the risk model ensuring the _residuals_ are maximally mean-reverting (or high Sharpe), rather than just minimizing reconstruction error.

## Tags

#AttentionMechanism #StatisticalArbitrage #EndToEndLearning #FactorModels #TransactionCosts #WeakFactors
