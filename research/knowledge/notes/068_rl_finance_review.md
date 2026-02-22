# Reinforcement Learning in Financial Decision Making: A Systematic Review

**Authors**: Mohammad Rezoanul Hoque, Md Meftahul Ferdaus, M. Kabir Hassan
**Date**: 2025
**Topic**: Reinforcement Learning, Systematic Review, Market Making, Portfolio Management

## Summary

A comprehensive review of 167 RL papers in finance (2017-2025). The study finds that RL confers the most significant advantage in **Market Making** and **Cryptocurrency Trading**, while Portfolio Management shows more modest gains. A key finding is that **Implementation Quality** (state space design, reward shaping, domain knowledge) correlates more strongly with performance than **Algorithmic Complexity** (e.g., choice of PPO vs DQN).

## Key Concepts

1.  **Market Making as "Killer App"**:
    - RL excels here due to the continuous nature of the problem and the need to balance multiple objectives (inventory, spread, risk) dynamically.
    - Highest "RL Premium" (performance gain over baseline) found in market making papers.
2.  **Implementation > Algorithm**:
    - Statistical analysis shows little difference between algorithm families (e.g., Policy Gradient vs DQN).
    - Success depends on _how_ it's applied: Feature engineering, simulation fidelity, and reward function design.
3.  **Hybrid Models**:
    - Combining RL with traditional models (e.g., using classical models for base rates and RL for residual correction) often works best.

## Implications for Our Platform

- **Strategic Focus**:
  - Continue focusing our RL efforts on **Market Making / Execution** rather than low-frequency portfolio picking.
- **Development Prioritization**:
  - Spend 80% of time on **Environment Design** (Simulator fidelity, Reward function, State features) and only 20% on "upgrading the algorithm" (e.g., SAC vs PPO).
  - "Better Data > Better Model".

## Tags

#ReinforcementLearning #SystematicReview #MarketMaking #AlgoTrading #BestPractices
