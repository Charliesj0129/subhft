# Intraday Limit Order Price Change Transition Dynamics Across Market Capitalizations Through Markov Analysis

**Authors**: Salam Rabindrajit Luwang et al. (NIT Sikkim, India)
**Date**: January 2026
**Topic**: Market Microstructure, Limit Order Book (LOB), Markov Chains, Price Inertia

## Summary

The paper applies Discrete-Time Markov Chains (DTMC) to model the sequence of price changes for limit orders in NASDAQ100 stocks. It stratifies stocks by Market Cap (High/Medium/Low) and time of day (6 intervals from Open to Close). The study focuses on "Price Inertia" (probability of remaining at the same price) and directional transitions.

## Key Concepts

1.  **Markov State Space**: 9 states defined by % price change magnitude (e.g., Neutral, Mild Buy +0.01%, Aggressive Buy +1%, etc.).
2.  **Price Inertia Patterns**:
    - **U-Shaped**: Inertia is highest at Market Open and Close (defensive positioning, portfolio rebalancing) and lowest at Midday (discovery phase).
    - **Capitalization Gradient**: High Cap (HMC) stocks have the highest inertia (stable liquidity). Low Cap (LMC) stocks have the lowest inertia (frequent re-pricing due to uncertainty).
3.  **Bid-Ask Asymmetry**:
    - LMC stocks show distinct asymmetry: **Ask Inertia > Bid Inertia**. This is attributed to inventory risk management by market makers and short-selling frictions.
4.  **Stationary Distributions**: The "long-run" probability is heavily skewed towards Neutral and Mild states.

## Implications for Our Platform

- **Execution Logic**: Our execution algorithms should adapt to the "Inertia Cycle".
  - **Open/Close**: Expect limit orders to stick. Good for "Join" strategies (wait in queue).
  - **Midday**: Expect frequent price jumping. Better for "Snipe" or "Aggressive" strategies.
- **LMC/Small Cap Strategy**: If trading lower liquidity coins/stocks, expect the Ask side to be "stickier" than the Bid side. We might need asymmetric logic for buying vs selling.
- **Feature Engineering**: We can calculate "Transition Entropy" or "Inertia Probability" (Markov $P_{ii}$) as a real-time feature. High inertia = stable regime; Low inertia = volatile/discovery regime.

## Tags

#MarketMicrostructure #LOB #MarkovChain #PriceInertia #ExecutionAlgo
