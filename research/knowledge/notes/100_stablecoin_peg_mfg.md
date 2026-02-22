# Who Restores the Peg? A Mean-Field Game Approach to Model Stablecoin Market Dynamics

**Authors**: Hardhik Mohanty, Bhaskar Krishnamachari
**Date**: 2026-01-26
**Topic**: Stablecoins, Mean-Field Games (MFG), Systemic Risk, Market Microstructure, De-peg

## Summary

The paper uses **Mean-Field Games (MFG)** to model the dynamics of Stablecoin De-pegging and Restoration. It explicitly models the interaction between **Arbitrageurs** (who can access Primary Market mint/redeem) and **Retail Traders** (who only trade Secondary).

- **Model**:
  - **State**: Mispricing $m_t$, Primary Backlog $L_t$, Secondary Flow $\phi_t$.
  - **Agents**: Continuum of Retail ($Cost \sim \text{Slippage}$) and Arbitrageurs ($Cost \sim \text{Gas} + \text{Slippage} + \text{RedemptionDelay}$).
  - **Dynamics**: Price evolves via net order flow + exogenous shock (GARCH volatility).
- **Calibration**:
  - Calibrated to 3 events: USDC (Mar 2023), USDT (May 2022), USDT (July 2023).
  - Matches historical **Half-Lives** of peg recovery.
- **Key Findings**:
  - **Threshold Effect**: There is a non-linear tipping point in **Primary Market Friction** (Redemption Cost/Delay). Below this threshold, arbitrageurs quickly restore the peg. Above it, primary arbitrage shuts down, and the burden shifts to secondary markets, which are insufficient, causing a prolonged de-peg.
  - **Who Restores?**: In normal stress, **Primary Arbitrage** does 80%+ of the work. In severe de-pegs (e.g., USDC Bank Run), **Secondary Market** flow becomes critical as primary rails get congested/paused.

## Key Concepts

1.  **Mean-Field Game (MFG)**:
    - Approximation for $N \to \infty$ agents. Instead of solving $N$ coupled strategies, each agent solves optimization against a "Mean Field" (Average Flow).
    - Efficient for modeling "Crowd Behavior" in crypto.
2.  **Primary vs. Secondary Restoration**:
    - Primary: Mint/Redeem 1:1. Slow, high fixed cost, but infinite capacity (theoretically).
    - Secondary: Buy/Sell on Binance/Curve. Fast, low fixed cost, but high price impact (limited capacity).

## Implications for Our Platform

- **Stablecoin Arb Strategy**:
  - **Signal**: Monitor **Primary Market Congestion** (e.g., Mint/Redeem lag or elevated Gas on Ethereum).
  - **Logic**: If Primary Friction > Threshold, the peg will NOT be restored quickly. **Do not** buy the dip immediately. Wait for the "Secondary Amplifier" phase or for primary friction to subside.
- **Risk Management**:
  - **De-peg Protection**: If holding stablecoins, monitor the "Exploitability" metric or Primary Backlog. If it spikes, hedge immediately into Fiat or Bitcoin, as restoration mechanism is failing.

## Tags

#Stablecoins #MeanFieldGames #DeFi #SystemicRisk #Arbitrage #MarketMicrostructure
