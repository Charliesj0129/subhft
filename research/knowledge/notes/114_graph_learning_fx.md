# Graph Learning for Foreign Exchange Rate Prediction and Statistical Arbitrage

**Authors**: Yoonsik Hong, Diego Klabjan (Northwestern University)
**Date**: 2025-08
**Topic**: Graph Learning, FX Statistical Arbitrage, Spatiotemporal GNN, Triangular Arbitrage, Execution Time Lag

## Summary

The paper proposes a **two-step Graph Learning (GL) approach** for FX trading, addressing the limitations of prior work that ignores multi-currency relationships and execution time lags.

- **Problem**:
  - **FX Rate Prediction (FXRP)**: Existing models (LSTM, Transformer) treat currency pairs (e.g., EURUSD) in isolation, ignoring the triangular constraints ($X_{ij}X_{jk}X_{ki}=1$) and interest rate parity (IRP).
  - **Statistical Arbitrage (FXSA)**: Triangular arbitrage execution entails a "Time Lag" between observing the price and executing the 3 legs. Most papers assume instant execution, leading to "Look-Ahead Bias" and risk.
- **Methodology**:
  - **Step 1: FXRP via GNN**:
    - Construct a graph where **Nodes = Currencies** and **Edges = Exchange Rates**.
    - Features: Interest Rates (Node features), FX Rates (Edge features).
    - **Maximum Likelihood Estimation (MLE)** of "Intrinsic Values" ($V_t^i$) used as auxiliary features.
  - **Step 2: FXSA via Stochastic Optimization**:
    - Formulate an optimization problem maximizing **Information Ratio** subject to "Net Flow = 0" constraints (stochastic due to time lag).
    - The GNN outputs trading weights $w_{ij}$ directly, satisfying constraints via **Projection and ReLU**.
- **Key Findings**:
  - The GL method achieves **61.89% higher Information Ratio** than benchmarks.
  - Demonstrates that incorporating the "Triangular Graph Structure" significantly improves next-step FX prediction accuracy.

## Key Concepts

1.  **Graph Representation of FX**:
    - Currencies are nodes. Exchange rates are directed edges.
    - Message Passing naturally enforces/learns triangular relationships (e.g., EUR $\to$ USD $\to$ JPY $\to$ EUR).
2.  **Stochastic Arbitrage with Time Lag**:
    - Instead of assuming $X_{ij}X_{jk}X_{ki} > 1$ risk-free, they model the _risk_ that prices move during execution ($t_{exec}$).
    - The objective is max **Risk-Adjusted Return** (Information Ratio), not just raw profit, explicitly penalizing execution variance.

## Implications for Our Platform

- **Graph-Based Alpha**:
  - We should construct a **Crypto Graph** (BTC, ETH, SOL, USDT) where edges are the exchange rates.
  - Use a **GAT (Graph Attention Network)** to predict price movements. If BTC moves, it "propagates" to ETH and SOL via the graph edges.
- **Arbitrage Execution**:
  - The paper's handling of **Execution Lag** is crucial. We should model the "Slippage Risk" of triangular arb (e.g., BTC-USDT-ETH-BTC) as a stochastic constraint, not a deterministic one.

## Tags

#GraphNeuralNetworks #FXTrading #StatisticalArbitrage #TriangularArbitrage #ExecutionRisk #SpatiotemporalGraph
