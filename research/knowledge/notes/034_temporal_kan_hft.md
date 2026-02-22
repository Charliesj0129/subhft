# Temporal Kolmogorov-Arnold Networks (T-KAN) for HFT

**Author**: Ahmad Makinde (University of Bristol)
**Date**: Jan 2026
**Topic**: Deep Learning, LOB Forecasting, Kolmogorov-Arnold Networks (KAN), Alpha Decay, FPGA

## Summary

The paper proposes **Temporal Kolmogorov-Arnold Networks (T-KAN)**, a novel architecture that integrates KAN layers into LSTM cells to improve high-frequency limit order book (LOB) forecasting. The key innovation is replacing fixed linear weights in LSTMs with learnable B-spline activation functions ("computation on the edges"), allowing the model to learn complex, non-linear market regimes more effectively than standard deep learning models like DeepLOB.

## Key Concepts

### 1. T-KAN Architecture

- **Problem**: Standard LSTMs use fixed activation functions (sigmoid/tanh) and linear weights, which struggle to capture fine-grained, non-linear microstructure dynamics, leading to rapid "alpha decay" at longer horizons ($k=100$).
- **KAN Layers**: Based on the Kolmogorov-Arnold representation theorem, KANs learn the activation functions themselves (parametrized as B-splines) on the edges of the network.
- **Hybrid Design**:
  - **Encoder**: Dual-layer LSTM (64 units) to capture temporal dependencies.
  - **Cell Modification**: The gating logic in the LSTM is transformed using KAN layers ($i_t, f_t, o_t$ use KANs instead of linear transforms).
  - **Head**: KAN-optimized classification head.

### 2. Performance & Alpha Decay

- **Dataset**: FI-2010 Benchmark.
- **Alpha Persistence**: T-KAN outperforms DeepLOB significantly at longer horizons ($k=100$).
  - **F1-Score**: 19.1% relative improvement (0.3995 vs 0.3354).
  - **Return**: In a backtest with 1.0 bps transaction costs, T-KAN achieved **+132.48%** return, while DeepLOB suffered a **-82.76%** drawdown.
- **Profitability Density**: Despite having more parameters (104k vs 58k), the "profit per parameter" is much higher, justifying the complexity.

### 3. Interpretability & Hardware

- **Learned Activations**: The model learned **S-curve B-splines** that naturally created "dead-zones" around zero-mean inputs.
  - **Noise Filtering**: This effectively acts as an learned filter for "bid-ask bounce" noise, only activating on high-conviction signals.
- **FPGA Suitability**:
  - Unlike Transformer/LSTM dense matrix multiplications, KAN layers rely on **localized B-spline evaluations**.
  - This structure is highly compatible with **High-Level Synthesis (HLS)** for FPGA implementation, promising sub-microsecond inference speeds.

## Implications for Our Platform

- **New Architecture Candidate**: We should consider testing T-KANs for our mid-price prediction models, especially if we face alpha decay at 10s-1min horizons.
- **FPGA Deployment**: The mention of HLS compatibility is crucial for our hardware acceleration roadmap. KANs might be cheaper to implement on FPGAs than large Transformers.
- **Noise Filtering**: The concept of "learned dead-zones" via B-splines is a powerful idea for filtering microstructure noise without manual thresholding.

## Tags

#DeepLearning #T-KAN #LOB #Forecasting #AlphaDecay #FPGA #Splines
