# AutoQuant: An Auditable Expert-System Framework for Execution-Constrained Auto-Tuning

**Authors**: Kaihong Deng
**Date**: December 2025
**Topic**: Backtesting, Execution Semantics, Funding Rates, Robustness, Bayesian Optimization

## Summary

The paper critiques the fragility of cryptocurrency perpetual futures backtests, noting that performance is often inflated by ignoring **Funding Rates** and **Execution Delays**. It proposes **AutoQuant**, a framework enforcing:

1.  **Strict T+1 Execution**: Signals calculated at bar close execute at the _next_ open.
2.  **Funding Awareness**: Explicit modeling of funding payments.
3.  **Double Screening**: A two-stage validation process involving Bayesian Optimization followed by Robustness Screening on a **Cost Grid**.

## Key Concepts

1.  **Strict T+1 Semantics**:
    - Prevents "Lookahead Bias" where a strategy executes at the same price used to generate the signal.
2.  **Funding-Aware Gating**:
    - Strategies should have dynamic thresholds based on Funding Rates.
    - If Funding is adverse (e.g., high fees to hold Long), the entry threshold for Longs should be raised ($ \tau*{long} = \tau*{base} + \kappa \cdot |Funding| $).
3.  **Double Screening Protocol**:
    - **Stage I**: Bayesian Optimization (TPE) to find candidates maximizing Net Return.
    - **Stage II**: **Cost Grid Screening**. Re-evaluates top candidates under stressed costs (e.g., 1.5x Fees, 2x Slippage, 3x Funding). Only candidates that survive _all_ scenarios are selected.

## Implications for Our Platform

- **Backtesting Engine**:
  - **Action 1**: Verify `t+1` enforcement. Our `Backtester` must typically execute orders on the `next_tick` after signal generation.
  - **Action 2**: Implement **Cost Grid Resilience**.
    - When optimizing a strategy, do not just pick the best Sharpe.
    - Run the "Best 10" strategies through a `stress_test(fees=1.5x, slippage=2x)`.
    - Discard any that fail (Sharpe < 1.0) in the stressed scenarios.
- **Strategy Logic**:
  - Add `funding_rate_threshold` to our Strategy parameters. Strategies should pause trading if Funding Rate > X (e.g. 0.05%/8h) in the direction of the trade.

## Tags

#Backtesting #ExecutionSemantics #FundingRates #Robustness #BayesianOptimization
