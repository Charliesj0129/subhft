# Autonomous Market Intelligence: Agentic AI Nowcasting Predicts Stock Returns

**Authors**: Zefeng Chen, Darcy Pu
**Date**: 2026
**Topic**: Agentic AI, LLM Trading, Stock Prediction, Factor Investing

## Summary

The paper investigates if a "Fully Agentic" LLM can predict stock returns without human curation.

- **Methodology**: They query a leading LLM daily (April 2025 - Jan 2026) for the entire **Russell 1000**. The AI autonomously web-searches and outputs an **Attractiveness Score** (-5 to +5).
- **Key Findings**:
  - **Asymmetric Skill**: The AI is excellent at picking **Top Winners** (Long Top 20 portfolio = 50% return vs 26% benchmark).
  - **No Skill in Losers**: The AI cannot reliably identify short candidates. Bottom-ranked stocks perform like the market.
  - **Daily Alpha**: The Top-20 portfolio generates ~18bps daily alpha (annualized Sharpe > 2.4).
  - **Reasoning**: Positive news (earnings, contracts) is unambiguous online. Negative news is obfuscated by management spin and social noise ("buy the dip").

## Key Concepts

1.  **Agentic Workflow**:
    - Prompt: "Search the web and rate this stock." No pre-fed news. This removes "Researcher Selection Bias".
    - **Nowcasting**: Predictions made _after_ close $t-1$ for _open_ $t$. Strict no-lookahead.
2.  **Signal Asymmetry**:
    - AI detects **Quality/Momentum** well.
    - AI struggles with **Distress/Shorts**.
3.  **Irreproducible Data**:
    - Because the AI searches the _live_ web, the experiment cannot be perfectly re-run later (search results change). This is a unique "Live" dataset.

## Implications for Our Platform

- **Alpha Strategy**:
  - **Long-Only Signal**: Use LLM-generated "Attractiveness" scores primarily for the **Long Leg** of a stat arb or portfolio strategy. Do not rely on it for shorting.
  - **Execution**: The paper trades Open-to-Open. We can likely improve this with intraday execution (VWAP).
- **Data Pipe**:
  - **Action**: Set up a daily cron job to query an LLM (e.g. Gemini/GPT-4o) for our universe.
  - **Prompt Engineering**: Use the "Agentic" style: "Search for latest news on AAPL and rate -5 to +5". Store this as a feature `llm_sentiment_score`.

## Tags

#AgenticAI #LLMTrading #StockPrediction #AlphaGeneration #Russell1000
