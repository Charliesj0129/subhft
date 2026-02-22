# End-to-End Policy Learning of a Statistical Arbitrage Autoencoder Architecture

**Authors**: Fabian Krause, Jan-Peter Calliess (University of Oxford)
**Date**: 2024-02
**Topic**: Statistical Arbitrage, Autoencoders, End-to-End Learning, Policy Gradient, Residual Trading

## Summary

The paper proposes an **End-to-End Learning** framework for Statistical Arbitrage, replacing the traditional two-step process (Factor Model $\to$ Residual Trading) with a single Autoencoder-based Policy Network.

- **Problem**:
  - Standard StatArb uses PCA/Fama-French to find factors, then models residuals as OU processes.
  - The Factor Model minimizes reconstruction error, NOT profit. The Trading Strategy maximizes profit but has no control over the factors.
- **Methodology**:
  - **Autoencoder for Factors**: Replaces PCA with an Autoencoder (AE) to capture non-linear market factors.
  - **Skip-Connection Policy**: The network computes residuals $\epsilon = Z - \text{Dec}(\text{Enc}(Z))$ internally.
  - **Policy Layer**: A tailored layer $w_t = \tanh(W^{(2)} \epsilon_t)$ maps residuals directly to portfolio weights.
  - **Joint Loss Function**:
    - $L = \text{MSE}(Z, \hat{Z}) - \lambda \cdot \text{Sharpe}(R_{portfolio})$
    - The model is forced to learn factors that explain market variance (MSE) _AND_ produce tradable, profitable residuals (Sharpe).
- **Key Findings**:
  - **End-to-End Wins**: The joint optimization outperforms both PCA-OU and standard Autoencoder-OU approaches.
  - **Modeling Risk Reduction**: Removes the need to manually select "Number of Factors" or "OU Thresholds". The network learns the optimal trade-off.

## Key Concepts

1.  **Differentiable Arbitrage**:
    - By making the entire pipeline differentiable, the "Trading Signal" can propagate gradients back to the "Factor Model".
    - If a certain factor helps explain variance but hurts profitability (e.g., non-mean-reverting residual), the Sharpe loss will suppress it.
2.  **Residual Layer**:
    - Explicitly coding the residual calculation $\epsilon = X - \hat{X}$ into the network architecture ensures the policy helps verify the "StatArb" hypothesis (trading mean-reversion of residuals).

## Implications for Our Platform

- **Model Architecture**:
  - We should build a `torch.nn.Module` that wraps our Factor Model (e.g., `Autoencoder` or `TFT`).
  - Add a `PolicyHead` that takes the residuals and outputs weights.
  - Train with a custom loss: `loss = reconstruction_loss - 0.1 * sharpe_ratio`.
- **Safety**:
  - The "Explained Variance" term (MSE) acts as a regularizer. If we only maximized Sharpe, the model might find spurious correlations. Forcing it to _also_ explain the market keeps the factors grounded in reality.

## Tags

#Autoencoder #EndToEnd #StatisticalArbitrage #DeepLearning #PolicyGradient #SharpeRatio
