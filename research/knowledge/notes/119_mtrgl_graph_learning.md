# MTRGL: Multi-modal Temporal Relational Graph Learning

**Authors**: Junwei Su, Shan Wu, Jinhui Li (Hefei Univ, HKU, UofT)
**Date**: 2024-01
**Topic**: Pair Trading, Temporal Graph Neural Networks (TGNN), Multi-modal Learning, Dynamic Graphs

## Summary

The paper introduces **MTRGL**, a framework for identifying **Pairs Trading** opportunities using **Temporal Graph Learning**.

- **Problem**:
  - Traditional Pair Trading relies on simple correlation (Cointegration) of price history.
  - This ignores **Multi-modal Features** (Sector, Volume, Market Cap) and treats relationships as static or purely statistical.
- **Methodology**:
  - **Dynamic Graph Construction**:
    - Nodes = Assets.
    - Edges = Created dynamically if the correlation $S(P_i, P_j)$ in the current window exceeds a threshold $\gamma$.
    - Edge Timestamp = Middle of the window.
  - **MTRGL Architecture (Memory-Based TGNN)**:
    - **Message Passing**: Updates node memory $s_i(t)$ based on interactions (edges).
    - **Memory Module**: Uses a GRU to evolve the state of each stock over time.
    - **Embedding**: Fuses the Memory State $s_i(t)$ with Static/Dynamic Node Features $X_i(t)$.
    - **Decoder**: A standard MLP that predicts the probability of a "link" (correlation) existing in the future.
- **Key Findings**:
  - MTRGL achieves **72.8% Average Precision** in predicting correlated pairs in KOSPI, significantly outperforming LSTM (61.4%) and Cointegration (55.8%) baselines.
  - **Ablation**: Removing the "Graph Structure" (treating stocks independently) or "Node Features" drastically reduced performance, proving the synergy of structural + feature learning.

## Key Concepts

1.  **Link Prediction as Pair Selection**:
    - Instead of regressing the spread, the model predicts the _existence_ of a profitable pair relationship (link) in the next window.
    - This reframes StatArb as a **Graph Topology Prediction** problem.
2.  **Memory-Based Graphs**:
    - Stock relationships change. A static graph is useless. A simple dynamic graph forgets history.
    - **Node Memory (GRU)** allows the model to remember "Index A and Index B usually move together" even if they briefly diverge (which is exactly when we want to trade).

## Implications for Our Platform

- **Graph-Based Pair Selection**:
  - We can use a GNN to select the **Universe of Pairs** for our StatArb strategies.
  - Currently, we might select pairs based on historical correlation. A GNN can predict _future_ correlation using disjoint features (e.g., "Same Sector" + "High Volume" + "Recent Divergence").
- **Link Prediction**:
  - Train a model to predict `IsCorrelated(StockA, StockB, t+1)`. If probability is high but current correlation is low $\to$ Divergence Opportunity.

## Tags

#GraphNeuralNetworks #PairTrading #TemporalGraphs #LinkPrediction #MultiModal
