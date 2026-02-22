# Resisting Manipulative Bots in Meme Coin Copy Trading

**Authors**: Yichen Luo, Yebo Feng, Jiahua Xu, Yang Liu
**Date**: 2026-01-26
**Topic**: Market Manipulation, Meme Coins, Copy Trading, Multi-Agent Systems, LLMs

## Summary

The paper proposes a **Multi-Agent System (MAS)** powered by LLMs (with Chain-of-Thought) to defend copy traders against manipulative bots in Meme Coin markets (specifically Solana/Pump.fun).

- **Context**: Copy trading is popular in meme coins, but "Smart Money" wallets are often manipulative bots (Bundlers, Snipers, Wash Traders) designed to dump on copiers.
- **Taxonomy of Bots**:
  - **Bundle Bots**: Creator buys significantly in the same block as launch (or uses fresh wallets to hide).
  - **Sniper Bots**: Buy in the first few blocks (0.4-2s on Solana).
  - **Bump Bots**: Repetitive buy/sell of same amount to inflate volume/visibility.
  - **Comment Bots**: LLM/Scripted hype in comments.
- **Solution**:
  - **Agents**:
    1.  **Coin Evaluation**: Checks for Bundle/Sniper marks.
    2.  **Wallet Selection**: Analyzes track record for _genuine_ skill vs wash trading.
    3.  **Timing**: Decides entry/exit.
  - **Performance**: The system achieved 14% avg return vs negative for naive copy trading.

## Key Concepts

1.  **Adversarial Copy Trading**:
    - The "Smart Money" you are copying might be the _Creator_ themselves in disguise, waiting for you to buy so they can dump.
    - **Gradual Bundle**: decoupling creation and buying to hide the link.
2.  **Detection Feature**:
    - **Flip Ratio**: (Number of Buy/Sell flips) / (Net Position Change). High ratio = Wash Trading/Bump Bot.

## Implications for Our Platform

- **Signal Filtering**:
  - **Action**: If we use "Follow Smart Money" strategies, we must implement the **Bump Bot Detection** (Flip Ratio) and **Sniper Detection** (Time from Launch) algorithms to filter out manipulative wallets.
  - **Risk**: Copying a "Bundler" is guaranteed negative EV (you are the exit liquidity).
- **Market Microstructure**:
  - **Solana Specifics**: Block time is ~400ms. "Sniper" is defined as execution within blocks 1-5.

## Tags

#MemeCoins #CopyTrading #MarketManipulation #Solana #MultiAgentSystems #LLM
