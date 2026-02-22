# Enhanced Momentum with Momentum Transformers

**Authors**: Max Mason, Waasi A Jagirdar, David Huang, Rahul Murugan (Columbia University)
**Date**: 2024
**Topic**: Momentum Transformer, Time Series Momentum, Equities, TFT (Temporal Fusion Transformer), Change Point Detection (CPD)

## Summary

The paper adapts the **Momentum Transformer** architecture (originally by Wood et al. for futures) to **Equities**.

- **Methodology**:
  - **Architecture**: Temporal Fusion Transformer (TFT). This combines:
    - **LSTM Encoder**: For local/sequential patterns.
    - **Multi-Head Attention**: To capture long-term dependencies (e.g., repeating regimes from months ago).
    - **Gated Linear Units (GLU) / GRN**: To suppress noise and select relevant features ("Variable Selection Network").
  - **Data**: Top 5 companies by market cap across SIC sectors (Equities).
  - **Change Point Detection (CPD)**: Attempted to add explicit "Regime Change" features (from a CPD algorithm) to the model inputs.
- **Key Findings**:
  - **Performance**: The vanilla TFT achieved an Annual Return of 4.14% and Sharpe of 1.12 over a volatile period (2020-2023), outperforming Long-Only and LSTM baselines.
  - **Equities are Harder**: The model performed worse than the original paper's application to Futures/Indices (Sharpe ~2.0). Equities have higher idiosyncratic noise and lower signal-to-noise ratio.
  - **CPD Failure**: Adding explicit "Change Point" features _hurt_ performance (likely due to noise or overfitting). A pure attention mechanism learned regimes better than explicit engineering.

## Key Concepts

1.  **Temporal Fusion Transformer (TFT) for Trading**:
    - Uniquely suited for finance because it handles **Static Covariates** (Sector, Asset Class) and **Time-Varying Knowns** (Day of Week) alongside observed Time Series.
    - **Interpretability**: Attention weights reveal _which past time steps_ mattered. Variable Selection weights reveal _which features_ mattered.
2.  **Seven Sins of/Quantitative Investing**:
    - The authors encountered (and fixed) classic issues: **Look-Ahead Bias** (using future split-adjusted prices incorrectly) and **Survivorship Bias** (using current S&P 500 constituents back in time). This serves as a reminder to rigorously check data pipelines.

## Implications for Our Platform

- **TFT Implementation**:
  - We should experiment with the **Temporal Fusion Transformer** from `pytorch-forecasting` or creating a custom `TFT` model in our `models/` directory.
  - It is superior to vanilla LSTMs for multi-horizon forecasting because of the attention mechanism and variable selection.
- **Feature Engineering**:
  - Don't over-engineer "Regime Change" indicators. Let the **Attention Mechanism** find the regimes implicitly. Adding noisy indicators can confuse the model.

## Tags

#Transformer #TFT #Momentum #Equities #DeepLearning #AttentionMechanism
