# Reinforcement Learning in Financial Decision Making: A Systematic Review

**Authors**: Mohammad Rezoanul Hoque et al. (University of New Orleans)
**Date**: 2025/2026
**Topic**: RL Review, Market Making, Algorithmic Trading, Portfolio Optimization, Hybrid Models

## Summary

This systematic review analyzes 167 articles (2017-2025) on RL in finance. It finds that **Market Making** is the most successful application area (highest performance premium), followed by crypto trading. Crucially, the study concludes that **algorithm choice (e.g., PPO vs DQN) matters less than implementation quality and domain knowledge**. Hybrid models (combining RL with traditional methods like LSTM or rule-based guards) consistently perform 15-20% better than pure RL.

## Key Concepts

### 1. Performance Drivers

- **Domain matters most**: Market Making > Crypto > Algo Trading > Portfolio Opt.
  - Market making benefits most because it is a continuous control problem with clear feedback (spread capture) and high-frequency interactions.
- **Algorithm is secondary**: There is no statistically significant difference in performance between Policy Gradient and DQN families across the board. Quality of state/action design > choice of optimizer.
- **Hybrid Superiority**: Hybrid approaches (e.g., `LSTM-DDPG`, `Attention-DDPG`) show a clear trend of outperforming pure "End-to-End" RL.

### 2. Challenges

- **Non-Stationarity**: Markets evolve, making policies trained on static history brittle.
- **Sample Efficiency**: RL needs huge data; financial data is limited.
- **Exploration vs Safety**: Random exploration is costly in live markets. Safe RL or offline RL is preferred.

### 3. Emerging Trends

- **Knowledge Spillover**: Techniques from market making (inventory management, bid-ask optimization) are being successfully transferred to execution and risk management.
- **Sim-to-Real**: Increasing focus on realistic simulators (like `hftbacktest`) to bridge the gap between backtest and live performance.

## Implications for Our Platform

- **Focus on Hybrid**: For our RL agent, we should not aim for a "pure" RL solution that learns everything from scratch. Instead, we should use RL to _tune_ parameters of a traditional strategy (e.g., RL adjusting the skew of a standard Avellaneda-Stoikov model) or use Hybrid architectures (e.g., Transformer encoding the state before passing to PPO).
- **Market Making Priority**: Since MM shows the highest "RL Premium", we are right to focus our RL efforts on the HFT/LOB side rather than low-frequency portfolio allocation.
- **Implementation Quality**: We should spend more time on feature engineering (state representation) and reward shaping (risk-adjusted rewards) than on swapping PPO for SAC/TD3.

## Tags

#ReinforcementLearning #Review #MarketMaking #HybridModels #AlgorithmicTrading #FinancialAI
