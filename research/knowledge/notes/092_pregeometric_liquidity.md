# Pregeometric Origins of Liquidity Geometry in Financial Order Books

**Authors**: Joao P. da Cruz
**Date**: 2026-01-27
**Topic**: Market Microstructure, Econophysics, Order Book Geometry, Emergent Liquidity, Graph Laplacian

## Summary

The paper proposes a radical "Pregeometric" framework where financial markets are modeled not as agents trading at prices, but as an **Inflationary Relational Network** (abstract graph of economic entities).

- **Theory**:
  - **No intrinsic Price/Time**: The underlying graph has no "price" or "time" coordinates.
  - **Projection**: Observables (Price, Liquidity) emerge only when an observer projects this high-dimensional graph onto a lower dimension (using the **Graph Laplacian** eigenvectors).
  - **Prediction**: Under minimal assumptions (single-scale hypothesis), the projected liquidity density (Order Book shape) must follow a **Gamma distribution** ($\rho(x) \propto x^\gamma e^{-\lambda x}$).
- **Empirical Evidence**:
  - Author fits this "Integrated Gamma" model to Level II data for US equities (AAPL, NVDA, etc.).
  - The fits are robust and explain the "hump" shape of liquidity away from the mid-price better than standard power laws or exponentials.

## Key Concepts

1.  **Emergent Price**:
    - Price is just a coordinate in the spectral embedding of the market graph.
    - "Returns" are geometric responses to graph updates (new nodes/edges), not random walks.
2.  **Order Book Shape**:
    - The convexity (low liquidity near mid, rising to a peak, then decaying) is a geometric inevitability of projecting a scale-free network onto a line, not necessarily a strategic choice by market makers.

## Implications for Our Platform

- **LOB Modeling**:
  - **Alpha**: If the "Gamma" shape is a structural attractor, deviations from it might represent **displaced liquidity** that will revert.
  - **Strategy**: Fit the parameters $(\gamma, \lambda)$ of the Gamma distribution to the current order book in real-time.
  - **Signal**: If the current book is "too flat" or "too steep" compared to the structural $\gamma$, anticipate a liquidity refill or liquidity flight.

## Tags

#Econophysics #MarketMicrostructure #OrderBook #GraphTheory #SpectralEmbedding #Liquidity
