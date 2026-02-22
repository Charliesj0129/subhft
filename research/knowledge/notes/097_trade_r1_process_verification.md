# Trade-R1: Bridging Verifiable Rewards to Stochastic Environments via Process-Level Reasoning Verification

**Authors**: Rui Sun, Yifan Sun, Sheng Xu, Li Zhao, Jing Li, Daxin Jiang, Cheng Hua, Zuo Bai
**Date**: 2026-01-08
**Topic**: Reinforcement Learning, RLVR, Financial LLMs, Reasoning Verification, Reward Hacking

## Summary

The paper introduces **Trade-R1**, a framework to train financial LLM agents using Reinforcement Learning (RL) without succumbing to "Reward Hacking" (overfitting to lucky market returns).

- **Problem**: In finance, rewards (returns) are stochastic. A bad decision can yield a positive return (luck), and a good decision can yield a loss. Standard RL blindly reinforces the outcome, leading to "hallucinated reasoning" where the model invents justifications for lucky momentum stocks.
- **Solution**: **Process-Level Reasoning Verification**.
  - **Triangular Verification**: Decomposes the check into 3 consistency scores:
    1.  **Evidence $\leftrightarrow$ Reasoning**: Is the logic grounded in facts?
    2.  **Reasoning $\leftrightarrow$ Decision**: Does the decision follow from the logic?
    3.  **Evidence $\leftrightarrow$ Decision**: Is the decision consistent with the raw data?
  - **Dynamic Semantic Reward (DSR)**: The reward function $R = r \cdot \text{Similarity}(Reasoning, Evidence)$.
    - If reasoning is bad (Similarity $\approx 0$), the financial reward is suppressed (variance reduction).
    - If reasoning is good, the reward is boosted.
- **Results**:
  - Tested on **A-Shares** and **US Stocks**.
  - **Market-Only RL** (Standard): High returns in-sample, but huge hallucination rate (22%) and poor cross-market generalization.
  - **Trade-R1 (DSR)**: Slightly lower in-sample returns but much higher reasoning consistency (97%) and superior out-of-sample generalization.

## Key Concepts

1.  **Reward Hacking in Finance**:
    - LLMs optimize for the proxy (return) by outputting whatever text historically correlated with "Up". They become "Momentum Machines" rather than "Reasoning Agents".
2.  **Asymmetric Gating**:
    - DSR formulation: $G(r, s) = r(0.5+s)$ for $r>0$.
    - If $s$ (semantic score) is low, the gradient is scaled down by $0.25\times$. If $s$ is high, scaled up by $1.5\times$. This filters out "Lucky" samples.

## Implications for Our Platform

- **LLM Alpha**:
  - **Strategy**: If we deploy LLMs for trading (e.g., Sentiment Analysis $\to$ Trade), we _must_ implement a "Reasoning Check" or the model will just learn to cheat.
  - **Implementation**: Use a cheaper model (Judge) to score the reasoning of the main Trading Model before executing or updating weights.

## Tags

#ReinforcementLearning #LLM #FinancialReasoning #RewardHacking #RLVR #RAG #TradeR1
