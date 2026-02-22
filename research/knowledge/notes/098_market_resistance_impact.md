# Trading with Market Resistance and Concave Price Impact

**Authors**: Nathan De Carvalho, Youssef Ouazzani Chahdi, Grégoire Szymanski
**Date**: 2026-02-05
**Topic**: Optimal Execution, Market Resistance, Concave Impact, Propagator Models, Game Theory

## Summary

The paper models Optimal Execution in a market where "Sophisticated Traders" (e.g., HFTs/Market Makers) detect metaorders and trade against them, creating **Endogenous Market Resistance**.

- **Mechanism**:
  - **Metaorder Impact**: A large order pushes price up (Transient Impact kernel $G$).
  - **Resistance**: Sophisticated traders detect the "overreaction" (mispricing $\alpha^r$) and trade against it (rate $r^u$).
  - **Fixed Point**: The resistance $r^u$ depends on the impact, and the impact depends on the net flow $u - r^u$. This creates a **Fixed-Point Equation** $r^u = U(G * (u - r^u))$.
- **Resulting Impact Shape**:
  - The interaction naturally generates a **Square-Root Law** for market impact (concave) even if the propagator is linear, provided the resistance function $U(x)$ is quadratic (which arises from maximizing profit against the impact).
  - **Optimal Strategy**: The paper derives a Stochastic Fredholm Equation for the optimal strategy $u^*$. Numerical results show that in the presence of resistance, optimal execution should be **more aggressive initially** (to get ahead of resistance) but then smooths out.

## Key Concepts

1.  **Endogenous Resistance**:
    - Market Impact isn't just a mechanical friction; it's an equilibrium outcome of other traders reacting to you.
    - If you trade too slowly, resistance adapts. If you trade too fast, you pay high temporary impact.
2.  **Square-Root Law Origin**:
    - This paper provides a microstructural foundation for why Impact $\propto \sqrt{Volume}$. It emerges from the game between the Metaorder and the Resistance.

## Implications for Our Platform

- **Execution Algo**:
  - **Anti-Gaming Logic**: Our execution algos (TWAP/VWAP) are likely being "gamed" by resistance.
  - **Action**: Integrate a "Resistance Factor" into the cost function. If we detect high resistance (price reverting executing against us), we should effectively **pause** or **randomize**, rather than pushing through.
- **Impact Model**:
  - **Model**: Use the "Resistance Propagator" model $I_t = \int G(t-s)(u_s - r^u_s) ds$ instead of the standard propagator.

## Tags

#OptimalExecution #MarketMicrostructure #GameTheory #MarketImpact #SquareRootLaw
