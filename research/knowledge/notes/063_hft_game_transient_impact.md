# High-Frequency Analysis of a Trading Game with Transient Price Impact

**Authors**: Marcel Nutz, Alessandro Prosperi
**Date**: 2025
**Topic**: High-Frequency Trading, Transient Price Impact, Trading Games

## Summary

The paper analyzes the high-frequency limit of an n-trader optimal execution game in discrete time with transient price impact. Traders face transient impact (Obizhaeva-Wang type) plus quadratic instantaneous trading costs. There is a unique Nash equilibrium where traders minimize expected execution costs. The discrete equilibrium strategies converge to a continuous-time equilibrium with specific initial and terminal block costs. The paper shows that even small instantaneous costs in discrete time lead to a robust continuous-time limit, avoiding strategy oscillations.

## Key Concepts

1.  **Transient Price Impact (Obizhaeva-Wang Model)**:
    - Price impact decays exponentially over time (`exp(-rho * t)`).
    - This implies that spreading trades out reduces overall impact.
2.  **Instantaneous Costs vs. Discrete Time**:
    - Without instantaneous costs, strategies oscillate wildly as `dt -> 0`.
    - Adding small quadratic costs stabilizes the equilibrium.
3.  **Endogenous Block Costs**:
    - The limit implies specific costs for initial and terminal block trades, emerging naturally from the discrete game.

## Implications for Our Platform

- **Execution Algorithm Design**:
  - **Action**: Implement transient price impact modeling in our execution logic.
  - **Cost Minimization**: Avoid rapid oscillating trades. Smooth execution is optimal.
  - **Block Trades**: Be aware of implicit costs at the start and end of execution windows.
- **Strategic Competitors**:
  - Recognize that other HFTs are likely playing a similar game. If they are liquidating, expect initial impact followed by decay.
  - We can potentially profit by providing liquidity against their structured liquidation blocks.

## Tags

#OptimalExecution #GameTheory #PriceImpact #HFT #MarketMicrostructure
