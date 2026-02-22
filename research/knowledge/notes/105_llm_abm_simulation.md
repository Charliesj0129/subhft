# Agent-Based Simulation of a Financial Market with Large Language Models

**Authors**: Ryuji Hashimoto, Takehiro Takayanagi, Masahiro Suzuki, Kiyoshi Izumi
**Date**: 2025-10
**Topic**: Agent-Based Simulation (ABM), Large Language Models (LLMs), Behavioral Finance, Loss Aversion, All-Time High Anomaly

## Summary

The paper introduces **FCLAgent** (Fundamental-Chartist-LLM Agent), a hybrid agent for financial market simulation. It combines **LLMs** (Llama 3.1 8B) for high-level strategic decisions (Buy/Sell direction) with traditional **Rule-Based** mechanisms for low-level execution (Price/Volume).

- **Motivation**: Traditional agents (e.g., FCNAgent) struggle to capture context-dependent behavioral biases like **Loss Aversion** and **Path Dependence** (e.g., anchoring to All-Time Highs).
- **Methodology**:
  - **Trading Intention (LLM)**: The agent constructs a textual prompt containing Portfolio state (Unrealized Gain/Loss), Market Condition (Price vs All-Time High), and Trading History. The LLM outputs "Buy" or "Sell".
  - **Order Execution (Rule-Based)**: Once the direction is set, the price is determined by the standard Chiarella & Iori (2004) formula (Fundamental + Chartist + Noise components).
- **Key Findings**:
  - **All-Time High Anomaly**: The simulation successfully reproduces the empirical anomaly where "nearness to all-time high negatively predicts future returns." Standard agents failed to replicate this.
  - **Behavioral Realism**: FCLAgents exhibit **Disposition Effect** (selling winners too early) and **Loss Aversion** naturally, without being explicitly programmed to do so, solely from the LLM's pre-trained knowledge of human behavior.

## Key Concepts

1.  **Hybrid Agent Architecture**:
    - LLMs are bad at precise math but good at "sentiment" and "context."
    - Solution: Use LLM for the _Qualitative_ decision (Direction) and Code for the _Quantitative_ decision (Price/Size).
2.  **Context-Dependent Loss Aversion**:
    - Human risk appetite depends on "Reference Points" (e.g., Purchase Price vs. All-Time High). The LLM naturally switches reference points based on the prompt context, unlike static utility functions.

## Implications for Our Platform

- **LLM-Based Simulation**:
  - We can enhance our internal simulator by adding a few "LLM Agents" to the pool. They will introduce realistic "irrationality" (FOMO, Panic Selling) that mathematical agents miss.
  - **Implementation**: Use a small, fast LLM (e.g., Llama-8B or even smaller quantized models) to generate Buy/Sell signals based on textual market summaries.
- **Behavioral Stress Testing**:
  - Test our Market Making strategies against these LLM agents. If the LLM agents "panic" near an All-Time High, does our strategy get run over?

## Tags

#ABM #LLM #BehavioralFinance #MarketSimulation #LossAversion #HybridAI #Llama3
