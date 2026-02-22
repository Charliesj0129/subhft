# Agent-Based Simulation of a Perpetual Futures Market

**Authors**: Ramshreyas Rao
**Date**: 2025
**Topic**: Market Simulation, Agent-Based Modeling (ABM), Crypto Perps, Funding Rates

## Summary

The paper presents an **Agent-Based Model (ABM)** of a Cryptocurrency Perpetual Futures market. It captures the unique mechanics of "Perps" (Funding Rate pegging) by simulating heterogeneous agents (Chartists, Noise Traders) interacting in a Limit Order Book. It replicates the empirical phenomenon where Perps trade at a premium because Longs are typically "Positional" (momentum) while Shorts are "Basis Traders" (harvesting funding).

## Key Concepts

1.  **Agent Types**:
    - **Chartists**: Trade based on Momentum (Moving Averages). Used for "Positional" trades (Directional).
    - **Noise Traders**: Random trading.
    - **Basis Traders**: Trade specifically to capture the **Funding Rate** spread (Premium/Discount).
2.  **The Peg Mechanism**:
    - The price "pegs" to Spot not because of a central force, but as an emergent property of Basis Traders arbitraging the deviation.
3.  **Market Bias**:
    - Simulation shows: If Longs are Directional (buy & hold) and Shorts are Basis Traders (hedging spot), the Perp naturally trades at a **positive premium** (Contango).

## Implications for Our Platform

- **Simulation Environment**:
  - **Action**: Adopt the code snippet (R/Python) from the Appendix to build a `PerpMarketSimulator`.
  - **Use Case**: Test our `MarketMaking` logic in this simulator.
  - **Scenario**: What happens if "Chartist" volatility increases? Does our MM algorithm survive the toxic flow?
- **Understanding Market Bias**:
  - We can exploit the "Structural Premium". If the market is dominated by Long Speculators, we should be slightly biased towards **Shorting** (seeking funding) but hedging strictly.

## Tags

#AgentBasedModeling #MarketSimulation #CryptoPerps #FundingRates #Microstructure
