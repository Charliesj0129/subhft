# Directional Liquidity and Geometric Shear in Pregeometric Order Books

**Authors**: João P. da Cruz
**Date**: 2026
**Topic**: Order Book Physics, Liquidity Geometry, Pregeometric Models

## Summary

The paper proposes a "Pregeometric" framework where the order book is not a collection of independent Bid/Ask curves, but a **single liquidity field** projected onto a price axis.

- **Decomposition**: Order book dynamics are decomposed into:
  1.  **Drift**: Rigid translation of the entire liquidity field (Price change). This is a "Gauge" degree of freedom.
  2.  **Shear**: A geometric deformation of the field that creates Bid/Ask asymmetry without moving the mid-price immediately.
- **Key Finding**: Under a "Single-Scale Hypothesis" (no intrinsic length scale other than distance to mid), the liquidity shape must follow a **Gamma Distribution** ($x^\gamma e^{-\lambda x}$).
- **Empirical Validation**: Tests on AAPL, MSFT, NVDA, etc. confirm that the Gamma profile fits Level 2 data better than standard power laws or exponentials.

## Key Concepts

1.  **Gauge Invariance**:
    - Moving the mid-price is like changing constraints in a coordinate system. The "Physical" object is the shape of the book relative to the mid.
2.  **Geometric Shear**:
    - Flow toxicity or imbalances are interpreted as a "Shear" deformation.
    - $\text{Shear} \neq \text{Price Move}$. A book can be heavily sheared (imbalanced) without moving if the "Drift" mode is zero. This explains why order flow imbalance is not always predictive of immediate returns.
3.  **Gamma Profile**:
    - Liquidity density $\rho(x) \propto x^\gamma e^{-\lambda x}$.
    - $\gamma$ controls curvature near the mid. High $\gamma$ = sharp wall. Low $\gamma$ = flat book.

## Implications for Our Platform

- **Market Making**:
  - **Action**: Instead of fitting Bid/Ask curves separately, fit a single Gamma distribution to the _entire_ book centered at Mid.
  - **Signal**: Track the **Shear Parameter** ($\gamma_{bid}$ vs $\gamma_{ask}$) as a signal. A divergence in Shear might precede a price Drift (Gauge adjustment).
- **Execution Algorithms**:
  - **Cost Model**: Use the Gamma profile to estimate market impact cost more accurately than the standard square-root law.
  - **Drift vs Shear**: If the book is "Shearing" but not "Drifting", it might be a temporary liquidity event (reversion likely). If it is "Drifting", the price is fundamentally moving.

## Tags

#MarketMicrostructure #OrderBookPhysics #GammaDistribution #LiquidityModelling #GeometricShear
