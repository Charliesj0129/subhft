# Alpha-R1: Alpha Screening with LLM Reasoning

**Authors**: Zuoyou Jiang et al. (Shanghai Jiao Tong University & StepFun)
**Date**: Dec 2025
**Topic**: LLM, Reinforcement Learning, Alpha Screening, Grit, GRPO, Factor Zoo

## Summary

The paper introduces **Alpha-R1**, an 8B-parameter reasoning model (based on Qwen3-8B) trained via Reinforcement Learning (RL) to perform **dynamic alpha screening**. Unlike traditional methods that treat factors as static numerical time-series, Alpha-R1 uses "economic reasoning" to contextually activate/deactivate factors based on market regimes (e.g., "value factors work best in high inflation").

## Key Concepts

### 1. The Reasoning Core

- **Semantic Profiling**:
  - **Factor Description ($\alpha_{des}$)**: LLM generates a semantic profile for each factor (mechanism, failure conditions) based on historical backtests.
  - **Market State ($S_t$)**: A daily summary of price technicals + news narratives.
- **Context-Aware Screening**: The model takes $\{ \alpha_{des}, S_t \}$ as input and outputs a selected subset of factors $A_t$.
- **Analogy**: It acts as a "semantic gating network" for a linear factor model, dynamically turning coefficients on/off based on the narrative fit.

### 2. Reinforcement Learning via GRPO

- **RLHF Adaptation**: Instead of human feedback, the reward comes from **objective market performance**.
- **Reward Function**: $R_{final} = R_{adjusted} - P_{structural}$
  - $R_{adjusted}$: Excess return of the selected factor portfolio over a 5-day holding period.
  - $P_{structural}$: Penalties for hallucinated factors or lack of sparsity.
- **GRPO (Group Relative Policy Optimization)**: Used instead of PPO to save memory and ensure stability. It optimizes the policy based on a group of sampled outputs, eliminating the need for a separate critic network.

### 3. Performance

- **Backtesting (2025 H1)**: Tested on CSI 300 (In-domain) and CSI 1000 (Out-of-domain).
- **Results**: Alpha-R1 achieved significantly higher returns and Sharpe Ratios than traditional ML (XGBoost, PPO) and generic reasoning LLMs (DeepSeek-R1, Claude 3.7 Thinking).
  - **CSI 1000**: +42.49% return vs -6.44% (PPO).
  - **Robustness**: Shows strong zero-shot transferability to small-cap stocks.

## Implications for Our Platform

- **Algorithm Upgrade**: We can implement a simplified "Alpha-R1" logic in our `llm_strategy_selector`. Instead of just asking the LLM "pick the best strategy", we should feed it:
  1.  **Factor/Strategy "User Manuals"** (Semantic descriptions).
  2.  **Daily Market "Sitrep"** (News + Technicals).
  3.  **Task**: "Select the valid subset for _today's_ regime."
- **GRPO for Finance**: This confirms GRPO is a viable, efficient method for fine-tuning our own small reasoning models (e.g., Qwen-7B) on our private backtest data without needing a massive PPO setup.

## Tags

#LLM #ReinforcementLearning #AlphaScreening #GRPO #FactorZoo #Qwen
