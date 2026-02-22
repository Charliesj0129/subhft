# Statistical Arbitrage in Options Markets by Graph Learning and Synthetic Long Positions

**Authors**: Yoonsik Hong, Diego Klabjan (Northwestern University)
**Date**: 2025-08
**Topic**: Options Statistical Arbitrage, Graph Learning, RNConv (Tree-based GNN), Synthetic Long/Short, Put-Call Parity

## Summary

The paper extends the Graph Learning approach to **Options Markets**, specifically targeting **Statistical Arbitrage** on the KOSPI 200 Index Options.

- **Problem**:
  - Direct identification of StatArb in options is rare (most focus on pricing).
  - Standard GNNs struggle with **Tabular Features** (Strike, Maturity, Implied Vol), where Tree-based models (XGBoost) usually win.
- **Methodology**:
  - **Prediction Target**: The price deviation of **Synthetic Zero-Coupon Bonds** constructed from Synthetic Longs ($C-P$) and the Underlying ($S$).
    - Payoff of $(\frac{1}{K} S - \frac{1}{K}(C-P))$ at maturity is exactly $1$.
    - Under No-Arbitrage, the price $\delta_{a, \tau}$ should be the same for all strikes $K$ with the same maturity. Deviations = Arbitrage.
  - **Architecture (RNConv)**: A novel GNN that incorporates **Differentiable Decision Trees (NODE)** into the graph convolution.
    - Nodes = Put-Call Pairs $(K, T)$. Edges = defined by closeness in Strike/Maturity.
    - RNConv leverages the tabular nature of option features better than MLP-based GNNs.
  - **Trading Strategy (SLSA)**: Synthetic-Long-Short-Arbitrage.
    - Go Long the cheap synthetic bond, Short the expensive synthetic bond.
    - Provably delta-neutral and risk-free if held to maturity (ignoring execution risk/margin).

## Key Concepts

1.  **Synthetic Zero-Coupon Bond**:
    - Constructed from options via Put-Call Parity: $S + P - C = K \cdot e^{-rT}$.
    - Any deviation from this identity across different strikes $K$ implies an arbitrage opportunity (Box Spreads, etc.).
2.  **RNConv (Revised Neural Oblivious Decision Ensemble Graph Convolution)**:
    - Merges GNNs (for relational structure between strikes) with NODE (for tabular feature processing).
    - State-of-the-art for options data where features are distinct/tabular.

## Implications for Our Platform

- **Options Arbitrage Scanner**:
  - We can implement the **Synthetic Bond** metric ($\delta_{a, \tau}$) for Deribit Options (BTC/ETH).
  - Compute $\delta_{K, T}$ for all active strikes. If $\delta_{K_1, T} \neq \delta_{K_2, T}$, there is a **Box Spread Arbitrage** opportunity.
- **Model**:
  - Instead of a vanilla GNN, use a **Tree-based GNN** (like RNConv or just XGBoost features fed into a GNN) for options pricing/prediction.

## Tags

#OptionsArbitrage #GraphLearning #RNConv #PutCallParity #SyntheticPositions #TreeBasedGNN
