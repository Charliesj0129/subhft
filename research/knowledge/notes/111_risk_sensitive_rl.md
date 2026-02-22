# Risk-sensitive Reinforcement Learning Based on Convex Scoring Functions

**Authors**: Shanyu Han, Yang Liu, Xiang Yu
**Date**: 2025-05
**Topic**: Risk-Sensitive RL, Convex Scoring Functions, Augmented State Space, Actor-Critic, Optimal Trading, Statistical Arbitrage

## Summary
The paper proposes a general framework for **Risk-Sensitive Reinforcement Learning** in finance. Instead of just maximizing expected return, the agent minimizes a *Risk Measure* defined by a **Convex Scoring Function**.
*   **The Problem**: Standard RL maximizes $E[\sum Rewards]$. Real traders care about risk (Variance, Expected Shortfall, Entropic VaR).
    *   Optimizing risk measures is often **Time-Inconsistent** (an optimal plan at $t=0$ may not be optimal at $t=1$).
*   **Methodology**:
    *   **Augmented State**: To resolve time-inconsistency, they augment the state space with the *Accumulated Cost/Return* so far: $Y_t = - \sum_{\tau=0}^{t-1} cost_\tau$.
    *   **Two-Stage Optimization**:
        1.  Inner Loop: Solve a standard MDP on the augmented state $(S_t, Y_t)$.
        2.  Outer Loop: Optimize an auxiliary variable $\upsilon$ (related to the risk threshold, e.g., VaR level).
    *   **Algorithm**: A custom **Actor-Critic** method where the Critic estimates the risk-adjusted value function $V(s, y, \upsilon)$.
*   **Application**:
    *   Applied to a **Statistical Arbitrage** pair trading problem (OU process).
    *   showed that agents trained with **Expected Shortfall (ES)** or **Variance** penalties avoided large drawdowns better than standard risk-neutral agents.

## Key Concepts
1.  **Convex Scoring Functions**:
    *   A generalized way to define risk. Examples:
        *   **Variance**: $f(y, \upsilon) = (y - \upsilon)^2$
        *   **Expected Shortfall**: $f(y, \upsilon) = \frac{1}{1-\alpha} (\max(0, y-\upsilon)) + \upsilon$
2.  **State Augmentation for Time Consistency**:
    *   By adding "Past PnL" ($Y_t$) to the state, a non-Markovian risk objective (like "Minimize variance of total terminal wealth") becomes Markovian.

## Implications for Our Platform
-   **Risk-Adjusted Rewards**:
    *   Instead of just `Reward = PnL`, we can use the **Augmented State** approach to train agents that explicitly minimize *Drawdown* or *Variance*.
    *   Feature Engineering: Add `Current_Episode_PnL` as a feature to the RL agent. This allows the agent to become "conservative" if it has already made good profits (lock-in) or "desperate" (if that's the desired behavior, though usually we want the opposite).
-   **Auxiliary Variable**:
    *   For VaR/CVaR optimization, the agent needs to learn the "VaR threshold" $\upsilon$ alongside the policy. We can add this as a learnable parameter in our PPO implementation.

## Tags
#RiskSensitiveRL #ExpectedShortfall #ActorCritic #ConvexScoring #StatisticalArbitrage #StateAugmentation
