# Manipulation in Prediction Markets: An Agent-based Modeling Experiment

**Authors**: Bridget Smart, Ebba Mark, Anne Bastian, Josefina Waugh
**Date**: 2026-01-28
**Topic**: Prediction Markets, Market Manipulation, Agent-Based Modeling, Whales, Herding

## Summary

The paper investigates the impact of "Whales" (participants with outsized budgets) on prediction market prices using an **Agent-Based Model (ABM)**.

- **Context**: Recent prediction markets (Kalshi, Polymarket) have increased position limits, allowing large traders to potentially manipulate prices.
- **Model**:
  - **Agents**: Heterogeneous bettors with varying **Budgets**, **Expertise** (signal quality), **Stubbornness** (learning rate), and **Bias**.
  - **Market**: Double auction (or simplified demand-based price update).
  - **Intervention**: Introduce a "Whale" with a biased valuation and large budget to see if they can distort the price away from the "True Election Outcome".
- **Key Findings**:
  - Whales can temporarily distort prices, especially if other agents are **Herding** (following price momentum) or are "Stubborn" (slow to update beliefs from private signals).
  - However, if the crowd is diverse and learns from signals, the market eventually "self-corrects" as the whale runs out of capital or the crowd arbitrages the distortion.
  - Distortion magnitude $\propto$ Whale Capital $\times$ Biased Valuation. Duration depends on crowd learning rates.

## Key Concepts

1.  **Whale Manipulation**:
    - A single large trader _can_ move the price, but sustaining a deviation requires immense capital against a crowd that has access to an independent signal (truth).
    - In the absence of herding, the crowd acts as a mean-reverting force against the manipulation.
2.  **Herding Amplification**:
    - If agents use current Price as a signal for Truth (Herding), they may follow the Whale's manipulation, creating a self-fulfilling prophecy or extending the distortion duration.
3.  **Agent Heterogeneity**:
    - Expertise ($e_i$): How noisy is the agent's signal?
    - Stubbornness ($s_i$): How much weight do they put on priors vs new signals?

## Implications for Our Platform

- **Market Integrity / Surveillance**:
  - **Feature**: We can detect "Whales" by monitoring net flow concentration.
  - **Strategy**: If we detect a manipulation (price divergence from independent models/polls without news), we can trade _against_ the whale (Mean Reversion strategy).
  - **Risk**: If the crowd herds, the mean reversion might be delayed.

## Tags

#PredictionMarkets #MarketManipulation #ABM #AgentBasedModeling #Whales #Herding #BehavioralFinance
