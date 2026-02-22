# Right Place, Right Time: Market Simulation-based RL for Execution Optimisation

**Authors**: Ollie Olby, Andreea Bacalum, Rory Baggott, Namid R. Stillman (Simudyne)
**Date**: 2025-11
**Topic**: Execution Algorithms, Optimal Execution, RL, Market Simulation, Efficient Frontier

## Summary
The paper presents an RL framework for adjusting execution schedules (VWAP/TWAP parameters) using a **Reactive Agent-Based Market Simulator**.
*   **Problem**: Optimizing execution algorithms (to minimize slippage) is hard because backtests don't capture **Market Impact**.
*   **Solution**:
    *   **Simulator**: Simudyne Pulse. Uses "Aggregate Impact Function" ($f_{mi}(Q) = \lambda Q^\gamma$) to model price impact of order flow.
    *   **RL Agent**: A policy gradient (REINFORCE) agent that learns the parameters $(\mu_k, \sigma_k)$ of a Gaussian execution schedule.
    *   **Decomposition**: The simulator can decompose Slippage $\zeta$ into **Market Risk** $\zeta_{MR}$ (exogenous) and **Market Impact** $\zeta_{MI}$ (endogenous).
*   **Key Findings**:
    *   **Pareto Efficiency**: The RL agent found strategies that lie closer to the **Almgren-Chriss Efficient Frontier** than standard TWAP/VWAP baselines.
    *   **Bi-modal Unbounded**: The best strategy found was a "Bi-modal Unbounded" distribution, concentrating execution in liquidity-rich periods (Open/Close) but adapting the timing based on expected impact.

## Key Concepts
1.  **Slippage Decomposition**:
    *   $\zeta = \zeta_{MR} + \zeta_{MI}$.
    *   Simulation allows measuring $\zeta_{MI}$ by comparing the realized price path against a "counterfactual" path where the agent didn't trade. This is impossible in real data.
2.  **Parametric Policy**:
    *   Instead of outputting action *every step*, the RL agent outputs the *parameters* of a execution profile (e.g., Gaussian means/variances) at the start. This makes the strategy **Interpretable** (compliant with EU AI Act) and easier to control.

## Implications for Our Platform
-   **Execution Improvement**:
    *   We should not just use static TWAP. We can train a "Scheduler Agent" that outputs the `Duration` and `Urgency` parameters for our execution algorithms based on current volatility.
-   **Impact Modeling**:
    *   Adopt the "Aggregate Impact Function" ($f(Q) = \lambda Q^\gamma$) in our backtester to simulate slippage more realistically than a fixed spread cost.
-   **Safety**:
    *   The paper emphasizes "Interpretable RL" (Parametric Policy). We should favor this over "Black Box" RL (end-to-end neural net) for execution, to ensure we can explain why the bot dumped inventory at a specific time.

## Tags
#ExecutionAlgo #OptimalExecution #MarketImpact #RL #Simudyne #EfficientFrontier #Slippage
