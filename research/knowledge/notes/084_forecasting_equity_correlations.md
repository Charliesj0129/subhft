# Forecasting Equity Correlations with Hybrid Transformer Graph Neural Network

**Authors**: Jack Fanshawe, Rumi Masih, Alexander Cameron
**Date**: 2026-01-08
**Topic**: Correlation Forecasting, Graph Neural Networks, Transformers, Statistical Arbitrage

## Summary

The paper presents a **"Temporal-Heterogeneous Graph Neural Network" (THGNN)** to forecast **10-day ahead stock correlations** for the S&P 500.

- **Architecture**:
  - **Temporal Encoder (Transformer)**: Encodes 30 days of history (returns, macro, technicals) into a node embedding.
  - **Relational Encoder (GAT)**: Propagates information across the stock network. Edges are weighted by previous correlations and sector links.
  - **Output**: Predicts the **residual** of the correlation in **Fisher-z space** (deviations from a 30-day rolling baseline).
- **Performance**:
  - **Forecasting**: Reduces MAE/RMSE compared to rolling historical baselines.
  - **Trading**: Used in a "SPONGEsym" clustering strategy. The forward-looking correlations produce baskets that adapt faster to regimes (e.g., COVID-19), yielding better risk-adjusted returns than backward-looking baselines.

## Key Concepts

1.  **Fisher-z Residual Prediction**:
    - Instead of predicting correlation $\rho \in [-1, 1]$ directly, predict $\Delta z = z_{future} - z_{rolling}$, where $z = \text{arctanh}(\rho)$.
    - This makes the target unbounded standard normal-ish, stabilizing gradients.
2.  **THGNN (Temporal-Heterogeneous GNN)**:
    - Combines **Time** (Transformer) and **Space** (Graph Attention).
    - Allows the model to learn that "Stock A and Stock B are correlated _when Volatility is High_" (Contextual Edge).
3.  **Forward-Looking Clustering**:
    - Traditional StatArb clusters based on _past_ correlations. This fails when correlations change (e.g., market crash).
    - Using _predicted_ correlations allows the clustering algorithm to "pre-position" the portfolio for the incoming regime.

## Implications for Our Platform

- **Risk Models**:
  - **Action**: Experiment with **Fisher-z residual prediction** for our covariance matrix forecasting.
- **StatArb**:
  - **Strategy**: Implement a "Forward-Looking Cluster" strategy. Use a simpler model (e.g., XGBoost on pair features) to predict 5-day correlation residuals, then re-cluster.
  - **GNN Spec**: The paper provides a good blueprint for a GNN architecture (Transformer -> GAT) if we decide to build deep learning models for market structure.

## Tags

#GraphNeuralNetworks #Transformers #CorrelationForecasting #StatisticalArbitrage #FisherZ #SPONGE
