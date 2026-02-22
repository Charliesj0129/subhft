# A Games-in-Games Paradigm for Strategic Hybrid Jump-Diffusions

**Authors**: Yunian Pan, Quanyan Zhu (NYU)
**Date**: December 2025
**Topic**: High-Frequency Trading, Game Theory, Regime Switching, Strategic Control

## Summary

The paper proposes a **Games-in-Games** (GiG) architecture for systems governed by **Regime-Switching Jump-Diffusions** (like financial markets). It models the market as two coupled games:

1.  **Inner Game (Micro)**: A continuous-time differential game (e.g., Inventory vs. Predator) played _within_ a specific market regime (e.g., Low Volatility).
2.  **Outer Game (Macro)**: A strategic game where "Macro Players" (e.g., Market Stabilizers vs. Attackers) influence the **transition probabilities** between regimes.

## Key Concepts

1.  **Two-Layer Hierarchy**:
    - **Inner Layer**: Solves a robust stochastic control problem (HJI Equation) to find optimal strategies (e.g., spreads, inventory) _given_ the current regime.
    - **Outer Layer**: Modulates the **Jump Intensities** ($ \mu\_{ij} $) of the hidden Markov chain to induce or prevent regime shifts, anticipating the inner layer's response.
2.  **Adaptive Spectral Gap**:
    - The system exhibits a "Two-Scale Turnpike" property.
    - **Strategic Switching**: The Outer Game effectively modulates the **spectral gap** of the regime-switching graph. If risks differ greatly between regimes, it accelerates transitions (high connectivity) to diffuse risk.
3.  **Application: Market Microstructure**:
    - **Inner Game**: Market Maker (MM) vs. Strategic Predator (SP). MM manages inventory; SP tries to push price against MM.
    - **Outer Game**: "Macro-Stabilizer" tries to keep market in "Normal" regime; "Macro-Attacker" tries to push it to "Flash Crash" regime.

## Implications for Our Platform

- **Regime-Aware Strategy**:
  - Our current strategies are likely "Inner Game" only (optimizing for current state).
  - We need an **Outer Loop** (`RegimeManager`) that anticipates regime shifts.
  - **Action**: Implement `RegimeSwitchingModel`. instead of just _reacting_ to high vol, calculate the **Transition Intensity** ($\mu_{ij}$) to predict _when_ a crash regime is likely.
- **Risk Management**:
  - Use the **Spectral Gap** concept: If the market's "connectivity" between stable and unstable regimes increases (transitions become frequent), **widen spreads** pre-emptively, even if current vol is low.

## Tags

#GameTheory #RegimeSwitching #MarketMicrostructure #HJIEquation #StochasticControl
