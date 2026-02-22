# Resisting Manipulative Bots in Memecoin Copy Trading: A Multi-Agent Approach with Chain-of-Thought Reasoning

**Authors**: Yichen Luo et al. (UCL, NTU)
**Date**: January 2026
**Topic**: Memecoin Trading, Copy Trading, Multi-Agent Systems, Bot Detection

## Summary

The paper analyzes the memecoin market (Solana, Pump.fun) and proposes a novel **"Multi-Agent" Copy Trading System** to filter out widespread scams and bots (Sniper, Bump, Bundle, Comment Bots). It uses separate LLM agents for Meme Evaluation (Content/Metrics), Trader Evaluation (KOL Behavior), Wealth Management (Allocation), and Execution (Sniping).

## Key Concepts

1.  **Manipulative Bots**:
    - **Bundle Bots**: Create 100+ wallets in the _same block_ as token creation to fake volume/holders.
    - **Bump Bots**: Repeatedly buy/sell small amounts to keep the token on the "Recent Activity" list.
    - **Comment Bots**: Spam generic "Bullish" messages.
    - **Sniper Bots**: Front-run liquidity pools.
2.  **Multi-Agent System**:
    - **Meme Agent**: Analyzes token metadata (CA verified? Dev holding? Twitter created just now?).
    - **Trader Agent**: Evaluates KOL wallets. Are they "insiders"? (bought in block 0/1). Are they "lucky"? (100% win rate on 1 token, 0% on others). Are they "Bots"? (execution speed).
    - **Result**: Agents outperform single LLMs significantly in P&L and precision.
3.  **Bot Detection Heuristics**:
    - **Block 0/1 Correlation**: Wallets created/funded by the same source in the same block = scam ring.
    - **Volume Consistency**: High volume with zero price movement = Bump/Wash trading.

## Implications for Our Platform

- **Bot Filtering Logic**: If we trade low-cap coins (or even mid-cap), we MUST filter out "fake volume".
  - `if unique_signers < threshold AND volume > huge_threshold`: flag as Bump Bot.
  - `if wallet_creation_time == token_creation_time`: flag as Insider/Dev Bundle.
- **KOL Selection**: Do not copy trade based on purely P&L. Filter out "Lucky/Insider" wallets by checking their entry timing (Block 0/1 is suspicious).
- **Multi-Agent Design**: Confirms that specialized agents (Evaluator vs Executor) work better than a monolithic "trading bot".

## Tags

#Memecoin #CopyTrading #BotDetection #MultiAgentSystem #PumpFun #Solana #ScamFilter
