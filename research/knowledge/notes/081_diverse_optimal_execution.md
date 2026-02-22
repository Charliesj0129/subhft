# Diverse Approaches to Optimal Execution Schedule Generation

**Authors**: Robert de Witt, Mikko S. Pakkanen
**Date**: 2026
**Topic**: Optimal Execution, Reinforcement Learning, Market Impact, Quality-Diversity

## Summary

The paper applies **MAP-Elites** (a Quality-Diversity algorithm) to optimal trade execution. Instead of training a single "best" agent, it generates a diverse _population_ of agents specialized for different market regimes (e.g., high vs low volatility).

- **Methodology**:
  - **Environment (GEO)**: A Gymnasium-based simulator calibrated to 400+ US equities with a **Transient Impact Model** (Propagator with exponential decay).
  - **Models**:
    - **PPO (Proximal Policy Optimization)**: A standard RL baseline.
    - **MAP-Elites**: Maintains a grid of policies. Dimensions = Volatility $\times$ Liquidity.
- **Results**:
  - **PPO-CNN** achieves **2.13 bps** arrival slippage vs **5.23 bps** for VWAP (significantly better).
  - MAP-Elites "Specialists" outperform the generalist PPO by **8-10%** within their specific niches.

## Key Concepts

1.  **Transient Impact Model**:
    - Impact isn't just instantaneous. It decays over time.
    - Model: $I_t = \sum G_0 e^{-\ell/\tau} \gamma (\frac{q}{V})^\beta$.
    - This "Propagator" model is crucial for realistic backtesting of execution algos.
2.  **Quality-Diversity (QD)**:
    - Classic RL finds one policy $\pi^*$ that maximizes _average_ reward.
    - QD finds a set of policies $\{\pi_1, \pi_2, ...\}$ such that $\pi_i$ is optimal for regime $i$.
    - Useful for trading because "High Vol / Low Liq" requires a very different strategy (aggressive sweep) than "Low Vol / High Liq" (passivejoin).
3.  **Gymnasium for Executing Optimally (GEO)**:
    - The authors built a robust Gym environment for this, handling order arrival, impact simulation, and benchmarking.

## Implications for Our Platform

- **Execution Algorithms**:
  - **Action**: Adopt the **Transient Impact Model** for our backtester. The exponential decay kernel is tractable and more realistic than square-root-only models.
  - **Alpha**: Train separate "Execution Agents" for different volatility regimes using the MAP-Elites concept. One agent for "Calm", one for "Panic".
- **Simulation**:
  - **Tool**: We should look for or replicate the `GEO` environment to test our Smart Order Router (SOR).

## Tags

#OptimalExecution #ReinforcementLearning #MarketImpact #MAPElites #AlgorithmicTrading #Gymnasium
