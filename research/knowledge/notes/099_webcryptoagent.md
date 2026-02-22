# WebCryptoAgent: Agentic Crypto Trading with Web Informatics

**Authors**: Ali Kurban, Wei Luo, Liangyu Zuo, Zeyu Zhang, Renda Han, Zhaolu Kang, Hao Tang
**Date**: 2026-01-08
**Topic**: Agentic Trading, Crypto, LLM, Risk Management, Self-Correction

## Summary

The paper presents **WebCryptoAgent**, a two-tier agentic framework for crypto trading that separates **Strategic Reasoning** (Hourly, LLM-based) from **Tactical Execution** (Second-level, Rule-based Risk Management).

- **Architecture**:
  - **Strategic Tier**: An LLM (GPT-5.2, Qwen-Max, etc.) analyzes multi-modal data (OHLCV, Technical Indicators, News, Social Sentiment) + **Contextual Memory** (retrieved similar past experiences) to generate a trade plan (Direction, Confidence, Rationale).
  - **Tactical Tier (Shock Guard)**: A fast, non-LLM risk layer that monitors high-frequency ticks. It triggers **Circuit Breakers** or **Stop Losses** immediately if volatility/drawdown exceeds thresholds, bypassing the slow LLM.
  - **Reflexion Loop**: After each trade, the agent generates a "Lesson" (Win/Loss attribution) and stores it in vector memory. Future decisions retrieve these lessons to avoid repeating mistakes.
- **Performance**:
  - Tested on BTC, ETH, POL (Polygon) from Jan 2025 to Jan 2026.
  - **Memory Effect**: Enabling memory generally improved stability and Sharpe ratio for GPT-5.2, but interestingly _hurt_ Qwen-Max (which overfitted to past specific setups).
  - **Best Model**: DeepSeek-Chat (without memory) and Qwen-Max showed strong results in specific regimes.

## Key Concepts

1.  **Vertical Decoupling**:
    - LLMs are too slow for HFT/Crypto execution.
    - **Solution**: Use LLM for _Strategy_ (Positioning) and specialized code for _Tactics_ (Execution/Risk).
2.  **Contextual Reflection**:
    - Storing "Lessons" ($e_t = (\text{Context, Outcome, Lesson})$) and retrieving them via RAG allows the agent to "learn" online without gradient updates.

## Implications for Our Platform

- **Agent Architecture**:
  - **Design Pattern**: Adopt the **Two-Tier** structure. Our "Strategy" agents (LLMs) should output a _Plan_ (e.g., "Buy, Stop at X, Target Y"), but the _Execution_ should be handled by the Rust engine (Risk Guard), not the LLM itself.
  - **Safety**: The "Tactical Shock Guard" is mandatory for any LLM-based trading to prevent hallucinated hold-during-crash scenarios.
- **Memory**:
  - **Feature**: Implement a simple `TradeLog` vector store where the agent writes a post-mortem after every closed trade. Retrieve top-3 similar past trades before opening a new one.

## Tags

#CryptoTrading #AgenticAI #LLM #RiskManagement #Reflexion #SystemArchitecture
