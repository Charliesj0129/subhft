# ProbFM: Probabilistic Time Series Foundation Model with Uncertainty Decomposition

**Authors**: Chinta et al. (JPMorgan Chase)
**Date**: January 2026
**Topic**: Time Series Foundation Models, Deep Evidential Regression (DER), Uncertainty Quantification (UQ)

## Summary

The paper introduces **ProbFM**, a Transformer-based Time Series Foundation Model that integrates **Deep Evidential Regression (DER)** to decompose uncertainty into _Epistemic_ (Model Unce) and _Aleatoric_ (Observable Unce). Unlike existing models (mixture models, conformal prediction), DER learns the parameters of a higher-order probability distribution (**Normal-Inverse-Gamma**) over the neural network weights in a single forward pass.

## Key Concepts

1.  **Uncertainty Decomposition**:
    - **Aleatoric**: Irreducible noise in the data (e.g. erratic price jumps).
    - **Epistemic**: Uncertainty in the model's parameters (e.g. regime shift, limited data).
    - **Inference**: $y \sim NIG(\mu, \lambda, \alpha, \beta)$. Prediction mean $\mu$, Variance $\frac{\beta}{\lambda(\alpha-1)}$.
2.  **Architecture**:
    - Adaptive Patching (transforms time series to patches based on frequency).
    - Standard Transformer Backbone.
    - **DER Head**: Outputs 4 scalars ($\mu, \lambda, a, b$) per time step.
3.  **Result**:
    - Outperforms Mean-Squared-Error (MSE) and standard NLL by correctly identifying "High Uncertainty" regimes where predictions are unreliable.
    - **Trading Strategy**: Filter trades where _Epistemic Uncertainty_ exceeds a threshold. This significantly improves Sharpe Ratio.

## Implications for Our Platform

- **Uncertainty-Aware Trading**:
  - Implement **DER Loss** (`NLL + KL_Regularization`) in our DeepXDE/RL models.
  - Filter signals: `if Epistemic_Uncertainty > Threshold: Stay Cash`.
  - This is crucial for **Regime Detection** (e.g. if unseen market condition, Epistemic spikes -> Stop Trading).
- **Single Pass Efficiency**: DER is computationally cheap compared to Ensembles or Bayesian NN (BNN), making it suitable for HFT.

## Tags

#ProbabilisticForecasting #DeepEvidentialRegression #UncertaintyDecomposition #Transformer #RiskManagement #CryptoTrading
