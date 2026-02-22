# Market Making and Transient Impact in Spot FX

**Authors**: Alexander Barzykin
**Date**: 2026-01-17
**Topic**: Market Making, Transient Price Impact, FX Markets, Propagator Model

## Summary

The paper extends the standard Avellaneda-Stoikov market-making model to specificities of **Spot FX** markets, particularly focusing on the **Transient** nature of market impact (Propagator model) rather than the permanent/linear impact of Almgren-Chriss.

- **Context**: FX Dealers trade OTC with clients (Skewing) and hedge in the Interbank market (Hedging).
- **Model**:
  - **Internalization**: Dealers internalize flow as much as possible.
  - **Impact**: Hedging in the interbank market causes **Transient Impact** (price jumps and decays exponentially, like Obizhaeva-Wang/Propagator).
  - **State Variable**: Introduces the **Resilient Impact State** $x_t$ into the HJB equation.
- **Key Findings**:
  - The optimal hedging speed $v^*$ and valid quotes $\delta^*$ depend on the current impact state $x_t$.
  - **Pure Internalization Zone**: The inventory band where the dealer _doesn't_ hedge widens or shifts based on the impact state. If the price is currently elevated due to recent buying (high $x_t$), the dealer is less likely to buy more (hedge short) because it's expensive.

## Key Concepts

1.  **Transient vs Permanent Impact**:
    - Empirical evidence in FX shows impact decays (mean-reverts).
    - Almgren-Chriss assumes permanent impact. This paper shows that for large trades/inventory shocks, neglecting decay leads to suboptimal hedging.
2.  **Impact State ($x_t$)**:
    - $dx_t = (-\beta x_t + k v_t) dt$. The impact state tracks the "pressure" on the price.
    - Optimal control becomes a function of $(q_t, x_t)$.
3.  **Quadratic Approximation**:
    - Solves the HJB using ansatz $V(t, q, x) = -A(t)q^2 - B(t)qx - C(t)$.
    - Result: Quotes $\delta^*$ are adjusted linearly by $x_t$ (if market impact is positive/high prices, bid/ask skew shifts).

## Implications for Our Platform

- **Execution/Hedging**:
  - **Action**: If we implement an inventory manager, we MUST track the "Impact State" of our own recent trades.
  - **Logic**: If we just bought a lot (driving price up), wait for $x_t$ to decay before buying more, or sell aggressively to capture the reversion.
  - **Model**: Adopt the propagator model ($dx = -\beta x dt + k dv$) for our transaction cost model in the backtester.

## Tags

#MarketMaking #FX #TransientImpact #PropagatorModel #StochasticControl #HJB
