# Temporal Kolmogorov-Arnold Networks (T-KAN) for High-Frequency Limit Order Book Forecasting

**Authors**: Ahmad Makinde
**Date**: 2026-01-05
**Topic**: High-Frequency Trading, Limit Order Book (LOB), Deep Learning, Kolmogorov-Arnold Networks, FPGA

## Summary

The paper introduces **Temporal Kolmogorov-Arnold Networks (T-KAN)** for forecasting LOB price movements, specifically the Alpha Decay problem in HFT.

- **Problem**: Alpha decays rapidly. Traditional Deep Learning models (like DeepLOB, CNN-LSTMs) use fixed activation functions (ReLU, Tanh) and linear weights, which may struggle with the highly non-linear, "path-dependent" nature of LOB dynamics over longer horizons ($k=100$ ticks).
- **Solution**:
  - **KAN**: Replaces linear weights with **Learnable B-Spline Activation Functions** on the edges.
  - **T-KAN**: Hybrids KAN with LSTM. The LSTM gates $(i_t, f_t, o_t)$ use KAN layers instead of linear projections.
- **Results**:
  - Tested on **FI-2010** dataset.
  - **F1-Score**: +19.1% improvement over DeepLOB at $k=100$.
  - **Profitability**: +132% return vs -82% for DeepLOB (after 1bps cost).
  - **Interpretability**: The learned splines show "dead-zones" (ignoring small noise) and non-linear amplification of large imbalances.

## Key Concepts

1.  **Kolmogorov-Arnold Representation**:
    - Any multivariate function can be represented as sums of univariate components.
    - KANs learn these univariate functions as Splines, allowing the network to learn the _shape_ of the microstructural response, not just the magnitude.
2.  **FPGA Suitability**:
    - KANs rely on localized B-splines (lookups/low-order polynomials) rather than massive dense matrix multiplications, making them potentially faster/more efficient for **FPGA** implementation (High-Level Synthesis) for sub-microsecond latency.

## Implications for Our Platform

- **Model Architecture**:
  - **Experiment**: Replace the MLP/Linear heads in our current Alpha models with **KAN Layers** (Splines).
  - **Library**: Check if `efficient-kan` or `pykan` is compatible with our PyTorch setup.
- **Hardware Acceleration**:
  - **Long-term**: If we move to FPGA, T-KAN might be a better candidate than Transformers.

## Tags

#HFT #DeepLearning #LOB #KAN #Splines #AlphaDecay #FPGA
