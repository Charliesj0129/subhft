# Learning Market Making with Closing Auctions

**Authors**: Julius Graf, Thibaut Mastrolia
**Date**: 2026-01-27
**Topic**: Market Making, Reinforcement Learning, Closing Auctions, Deep Q-Learning

## Summary

The paper extends the optimal market-making problem to include a **Closing Auction** phase. Most existing models liquidate inventory at the end of the continuous session with a penalty, ignoring the liquidity available in the closing auction.

- **Methodology**:
  - **Phases**: Continuous Trading (Limit Order Book) $\to$ Closing Auction.
  - **RL Agent**: Uses **Deep Q-Learning (DQN)** to learn a policy that anticipates the auction.
  - **State**: Includes projected clearing price of the auction ($H_{cl}$) during the continuous phase.
  - **Action**: Sets limit orders during continuous phase; submits supply schedule ($K, S$) during auction.
- **Key Findings**:
  - Anticipating the auction significantly improves PnL compared to standard inventory-penalized liquidation.
  - The agent learns to "hold" inventory for the auction if the projected clearing price is favorable, rather than dumping it at unfavorable spreads in the continuous market.

## Key Concepts

1.  **Projected Clearing Price**:
    - The agent estimates the auction clearing price _during_ the continuous phase by treating the current LOB snapshot as a fictitious auction.
    - Algorithm: Compute slope $\hat{K}$ for each price level based on standing volume, solve for clearing price $\tilde{S}$, and smooth it.
2.  **Auction Mechanics**:
    - The auction matches supply and demand at a single price $S_{cl}$ to maximize volume.
    - The agent submits a **Linear Supply Function** $g(p) = K(p - S)$.

## Implications for Our Platform

- **Execution Strategy**:
  - **Action**: If we have a large position to unload near close, do _not_ just use a TWAP/VWAP that ends at 15:59.
  - **Feature**: Implement a "Projected Closing Price" estimator (as per Algorithm 1) to decide how much to keep for the auction (MOC - Market On Close).
- **Market Making**:
  - **Risk**: The closing auction is a huge liquidity event. Our MM bots should participate to flatten inventory risk-free (or low risk) compared to holding overnight.

## Tags

#MarketMaking #ReinforcementLearning #ClosingAuction #DeepQNetwork #Liquidity
