# ProbFM: Probabilistic Time Series Foundation Model with Uncertainty Decomposition

**Authors**: Arundeep Chinta, Lucas Vinh Tran, Jay Katukuri (JPMorganChase)
**Date**: 2026-01
**Topic**: Time Series Foundation Models (TSFM), Deep Evidential Regression (DER), Uncertainty Decomposition (Aleatoric vs Epistemic), Normal-Inverse-Gamma (NIG) Prior

## Summary
The paper introduces **ProbFM**, a Time Series Foundation Model that integrates **Deep Evidential Regression (DER)** to provide valid uncertainty intervals without sampling.
*   **The Problem**: Existing TSFMs (like Lag-Llama, TimeGPT) provide uncertainty but conflate **Aleatoric** (noise) and **Epistemic** (model ignorance) uncertainty, or rely on slow sampling/quantiles.
*   **Methodology**:
    *   **Architecture**: PatchTST-style Transformer backbone.
    *   **Head**: Instead of outputting $\mu, \sigma$, it outputs parameters of a **Normal-Inverse-Gamma (NIG)** distribution $(\mu, \lambda, \alpha, \beta)$.
    *   **Uncertainty Decomposition**:
        *   **Epistemic** (Model Uncertainty): $Var[\mu|x] = \frac{\beta}{(\alpha-1)\lambda}$. High when the model hasn't seen similar data.
        *   **Aleatoric** (Data Noise): $E[\sigma^2|x] = \frac{\beta}{\alpha-1}$. High when the market is volatile.
    *   **Loss Function**: Negative Log Likelihood of the Evidence + **Coverage Loss** (to ensure 95% CI contains 95% of data) + Evidence Regularization.
*   **Results**:
    *   Applied to Crypto Forecasting.
    *   **Uncertainty-Aware Trading**: Filtering trades where *Epistemic* uncertainty is high (avoiding "unknown unknowns") improved Sharpe Ratio significantly compared to filtering by total variance.

## Key Concepts
1.  **Deep Evidential Regression (DER)**:
    *   Learning the "distribution of the distribution parameters".
    *   Allows single-pass uncertainty estimation.
2.  **Epistemic vs Aleatoric in Trading**:
    *   **Aleatoric High**: Market is volatile. Strategy might still work (e.g. Volatility Arb).
    *   **Epistemic High**: Model is confused/OOD. **STOP TRADING**. This distinction is crucial for HFT safety.

## Implications for Our Platform
-   **Safety Module**:
    *   Implement a **DER Head** on our price featurizer.
    *   **Circuit Breaker**: If $U_{epistemic} > Threshold$, HALT all automated trading. This detects "Flash Crashes" or "New Regimes" where the model is untrained, preventing catastrophic loss.
-   **Implementation**:
    *   Change the final layer of our models to output 4 scalars $(\mu, \lambda, \alpha, \beta)$ and use the NIG Loss.
    *   This is a "Zero-Cost" upgrade (same inference speed) but adds massive safety capabilities.

## Tags
#DeepEvidentialRegression #UncertaintyQuantification #Transformer #ProbabilisticForecasting #RiskManagement #EpistemicUncertainty
