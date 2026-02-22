# Advanced Statistical Arbitrage with Reinforcement Learning

**Authors**: Boming Ning, Kiseop Lee (Purdue University)
**Date**: March 2024
**Topic**: Statistical Arbitrage, Reinforcement Learning, Mean Reversion Trading, Empirical Mean Reversion Time (EMRT)

## Summary

The paper introduces a model-free, reinforcement learning (RL) framework for statistical arbitrage, specifically designed to address limitations in traditional parametric models like Ornstein-Uhlenbeck (OU) or Cointegration. The core innovation is the introduction of **Empirical Mean Reversion Time (EMRT)** to optimize spread construction without relying on specific stochastic process assumptions, and a Q-learning based strategy for execution.

## Key Concepts

### 1. Empirical Mean Reversion Time (EMRT)

- **Problem**: Traditional methods assume spreads follow an OU process, estimating parameters via Maximum Likelihood Estimation (MLE). Real markets often deviate from these assumptions.
- **Solution**: Define EMRT as the average time taken for a spread to revert to its long-term mean from a local extreme.
- **Methodology**:
  - Identify significant local minima/maxima based on a standard deviation threshold.
  - Track time intervals between these extremes and the subsequent crossing of the sample mean.
  - Optimize portfolio coefficients ($B$) specifically to **minimize this EMRT**, ensuring the fastest possible mean reversion.

### 2. RL Trading Strategy

- **State Space ($S_t$)**: Instead of raw prices or simple deviations, the state is defined by a vector of recent price trends (percentage changes over a lookback window $l=4$).
  - Discretized into 4 levels: +2 (Significant Up), +1 (Moderate Up), -1 (Moderate Down), -2 (Significant Down).
  - Total states: $4^l$ (e.g., 256 states for $l=4$).
- **Action Space ($A_t$)**:
  - Long (+1)
  - Neutral (0)
  - Short (-1) (Note: The paper's implementation essentially flips between Long/Neutral, or Long/Short depending on current holding constraints).
- **Reward Function ($R_t$)**:
  - $R_{t+1} = A_t \cdot (\theta - X_t) - c \cdot |A_t|$
  - Reward is positive when buying below the mean or selling above it. explicitly accounts for transaction costs ($c$).
- **Algorithm**: Q-Learning with $\epsilon$-greedy exploration.

### 3. Experimental Results

- **Data**: S&P 500 sector pairs (e.g., MSFT-GOOGL, V-MA, etc.) from 2022 (Formation) to 2023 (Trading).
- **Comparison**:
  - **Distance Method (DM)**: Standard sum of squared deviations + threshold rules.
  - **OU Method**: MLE estimation of OU parameters + derived entry/exit thresholds.
  - **Proposed RL Method**: EMRT-based formation + Q-learning execution.
- **Performance**:
  - The RL method consistently achieved higher **Sharpe Ratios** and **Cumulative Returns** across most pairs.
  - It showed resilience in drawdown periods where traditional methods suffered.
  - Example: In MSFT-GOOGL, RL achieved 37% return vs 11% (DM) and 8% (OU).

## Implications for Our Platform

- **Alpha Factor**: EMRT can be implemented as a new alpha factor or filter. We can measure the "speed of reversion" for any spread portfolio.
- **Strategy Design**: The discretization of price trends into "regime states" (+2/+1/-1/-2) is a simple but effective way to feed market dynamics into an RL agent without complex feature engineering.
- **Model-Free Approach**: Moving away from strict OU assumptions is valuable for crypto markets where "steady mean reversion" is rare and regimes shift quickly.

## Tags

#StatisticalArbitrage #ReinforcementLearning #MeanReversion #QLearning #EMRT #SignalProcessing
