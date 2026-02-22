# Technology Adoption and Network Externalities in Financial Systems

**Authors**: Tatsuru Kikuchi
**Date**: 2026-01-12
**Topic**: Technology Adoption, Network Externalities, Financial Networks, SWIFT gpi, Feynman-Kac

## Summary

The paper models technology adoption in financial networks using a **Spatial-Network Master Equation** that incorporates:

1.  **Spatial Spillovers**: Geographic neighbors adopting.
2.  **Network Spillovers**: Business partners (e.g., correspondent banks) adopting.
3.  **Interaction**: Amplification when a neighbor is _also_ a partner.

- **Methodology**:
  - Models adoption intensity $\tau(x, \alpha, t)$ as a diffusion process.
  - Solves it using the **Feynman-Kac formula**, representing adoption as the expected cumulative exposure to shocks along stochastic paths.
  - **Levy Jump-Diffusion**: Extends the model to capture "Critical Mass". Below threshold $\to$ gradual diffusion. Above threshold $\to$ jumps (cascades).
- **Empirical Application**:
  - Analyzes **SWIFT gpi** adoption (2017) among 17 G-SIBs.
  - Finds that **Network-Central** banks adopt earlier.
  - **founding members** (29% of banks) caused 39% of total system amplification.

## Key Concepts

1.  **Adoption Amplification Factor ($A_i$)**:
    - How much does a shock to node $i$ affect the _total system_?
    - $A_i = \text{Total Adoption} / \text{Initial Adoption}$.
    - This metric identifies "Technology Leaders" better than simple centrality.
2.  **Critical Mass Dynamics**:
    - Adoption is slow/linear until a threshold $\bar{\tau}^*$ is reached, then it jumps (exponential/cascade).
    - Policy Implication: Subsidies must push the system _over_ the threshold to be effective.

## Implications for Our Platform

- **Market regime modeling**:
  - **Analogy**: The "Critical Mass" dynamic (Diffusion $\to$ Jumps) maps well to **Market Crashes** or **Liquidity Crises**.
  - **Model**: We can use the Levy Jump-Diffusion framework to model transition from "Normal Volatility" to "Crisis".
- **Network features**:
  - **Feature**: For crypto assets, "Network Centrality" (e.g., number of exchanges listed, number of pairs) could be a predictor of "Adoption/Price Jumps".

## Tags

#NetworkEconomics #FinancialNetworks #TechnologyAdoption #SWIFT #SystemicRisk #DiffusionModels #FeynmanKac
