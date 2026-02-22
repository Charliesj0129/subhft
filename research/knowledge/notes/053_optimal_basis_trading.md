# Optimal Dynamic Basis Trading

**Authors**: B. Angoshtari, T. Leung (University of Washington)
**Date**: May 2019
**Topic**: Basis Trading, Stochastic Control, HARA Utility

## Summary

The paper presents an optimal dynamic strategy for trading the **futures basis** (the spread between spot and futures prices), modeled as a **Stopped Scaled Brownian Bridge**. This model accounts for the realistic scenario where the basis _tends_ to converge but may not fully close at maturity (non-convergence risk).

## Key Concepts

1.  **Stopped Scaled Non-Convergent Basis**:
    - Unlike traditional models (Ornstein-Uhlenbeck) that mean-revert to 0 or a constant, this model forces the basis towards 0 as maturity approaches (scaled Brownian bridge) but allows for a residual gap at maturity ($T + \epsilon$).
    - Dynamics: $dZ_t = -\frac{\kappa Z_t}{T-t+\epsilon} dt + \sigma dW_t$.
2.  **Optimal Control**: The optimal strategy is derived by solving the HJB equation for a HARA utility function.
    - **Feedback Control**: The optimal position depends on current wealth $X_t$, risk tolerance $\delta(x)$, and the current basis level $Z_t$.
    - Formula involves complex time-dependent functions $f(t), g(t), h(t)$ derived from a Riccati equation.
3.  **Nirvana Strategies**: Under certain conditions (high risk tolerance), the expected utility can explode to infinity ("Nirvana"), implying extremely leveraged bets. The paper identifies the conditions to avoid this.

## Implications for Our Platform

- **Basis Arb Strategies**:
  - We should replace our simple "Linear Mean Reversion" logic with this **Time-Dependent Mean Reversion** ($1/(T-t+\epsilon)$ term).
  - **Action**: Update our `basis_arb_strategy` to use the derived feedback control law (Eq 3.7 in the paper).
  - **Risk**: Specifically monitor the `non_convergence_risk` parameter $\epsilon$. If realized convergence is slower than model implies, reduce position size.
- **Implementation**:
  - Compute the functions $g(t)$ and $h(t)$ offline (or once per day) and use them for real-time position sizing.

## Tags

#BasisTrading #StochasticControl #OptimalExecution #FuturesQuantitative #HARAUtility
