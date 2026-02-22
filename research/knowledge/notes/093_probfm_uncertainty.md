# ProbFM: Probabilistic Time Series Foundation Model with Uncertainty

**Authors**: Arundeep Chinta, Lucas Vinh Tran, Jay Katukuri
**Date**: 2026-01-15
**Topic**: Time Series Foundation Models (TSFMs), Uncertainty Quantification, Deep Evidential Regression, Probabilistic Forecasting

## Summary

The paper introduces **ProbFM**, a Transformer-based Time Series Foundation Model that integrates **Deep Evidential Regression (DER)** to provide principled uncertainty quantification.

- **Problem**: Existing TSFMs (TimeGPT, Lag-Llama, MOIRAI) conflate uncertainty sources or rely on restrictive assumptions (e.g., fixed Student-T, Mixture Models). They lack a clean split between **Aleatoric** (noise) and **Epistemic** (model ignorance) uncertainty.
- **Solution**:
  - **Architecture**: PatchTST/Transformer backbone + DER Head.
  - **DER**: Learns a distribution over distribution parameters (Normal-Inverse-Gamma prior).
  - **Output**: Single forward pass yields point prediction $\mu$, Aleatoric uncertainty (expected data variance), and Epistemic uncertainty (variance of the mean).
- **Evaluation**:
  - Tested on **Crypto Returns** (11 assets, 2020-2025).
  - Compared DER vs Gaussian NLL, Quantile Loss, etc., on a fixed LSTM backbone to isolate the method's value.
  - **Trading**: Used uncertainty decomposition to filtered trades. Avoiding trades with high Epistemic uncertainty (unknown accumulation/regime) improved Sharpe Ratios.

## Key Concepts

1.  **Epistemic vs Aleatoric**:
    - **Aleatoric**: Market noise (volatility). High in crypto. Irreducible.
    - **Epistemic**: "I don't know". Model hasn't seen this pattern before. Reducible with data. High during regime shifts or OOD events.
    - **ProbFM** distinguishes them.
2.  **Deep Evidential Regression (DER)**:
    - Instead of predicting $(\mu, \sigma)$, predict the parameters $(\gamma, v, \alpha, \beta)$ of a Normal-Inverse-Gamma distribution.
    - Loss function penalizes "unjustified" evidence (high confidence when wrong).

## Implications for Our Platform

- **Risk Management**:
  - **Feature**: Implement DER heads on our alpha models.
  - **Logic**: If **Epistemic Uncertainty** is high, _reduce size_ or _stay out_. This detects "Black Swans" or "New Regimes" better than standard volatility (Aleatoric).
- **Forecasting**:
  - **Model**: Replace standard MSE/MAE loss with Evidence Loss (NIG) for our return predictors.

## Tags

#TimeSeries #FoundationModels #UncertaintyQuantification #DeepEvidentialRegression #CryptoTrading #ProbabilisticForecasting
