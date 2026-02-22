# PredictionMarketBench: A SWE-bench-Style Framework for Backtesting Trading Agents

**Authors**: Avi Arora, Ritesh Malpani
**Date**: 2026-01-28
**Topic**: Prediction Markets, Trading Agents, Benchmarking, LLMs, SWE-bench

## Summary

The paper introduces **PredictionMarketBench**, a benchmark for evaluating trading agents (including LLM-based ones) on **Prediction Markets** (specifically Kalshi).

- **Motivation**: Prediction markets are unique (binary payoffs $0/$1, probability interpretation, high fees). Existing evaluations lack standardized execution realism.
- **Framework**:
  - **Episodes**: Self-contained packages of historical data (Orderbooks, Trades, Settlement) from Kalshi.
  - **Simulator**: Deterministic, event-driven replay with **Maker/Taker** execution semantics and explicit **fee modeling** (critical in binary markets).
  - **Agent Interface**: Tool-based usage (get_markets, place_order) suitable for LLMs.
- **Key Findings**:
  - **LLM Agent (gpt-4.1-nano)**: Performed poorly (-2.77% return, large max DD). It traded too aggressively and ignored fee drag.
  - **Bollinger Bands**: Profitable (+1.67% return). Simple mean-reversion with GTC Limit orders (to capture Maker rebates/lower fees) worked best in volatile episodes (e.g., Crypto).
  - **Random Agent**: Lost money slowly.

## Key Concepts

1.  **Binary Contract Nuances**:
    - Payoff is all-or-nothing. Prices are probabilities ($0-99\cent$).
    - **Fees**: Taker fees are high (e.g., 7%). Maker fees are lower (e.g., 1.75%).
    - **Implication**: Strategies must be "Fee-Aware". Capturing the spread (Maker) is often the only way to profit; crossing the spread (Taker) requires massive edge (>7%).
2.  **Deterministic Replay**:
    - Unlike market interaction (live) or simple backtests (close-to-close), this benchmark replays every LOB update and trade print to simulate queue priority and realistic fills for Limit orders.

## Implications for Our Platform

- **Backtesting Infrastructure**:
  - **Action**: Adopt the "Episode" format (parquet + metadata) for our own backtesting of specific scenarios (e.g., "Flash Crash 2025", "Election Night").
  - **Sim Execution**: Ensure our simulator correctly handles **Maker vs Taker fees**. In crypto and prediction markets, this difference is the difference between profit and bankruptcy.

## Tags

#PredictionMarkets #Benchmarking #LLMAgents #Backtesting #Kalshi #MarketMicrostructure
