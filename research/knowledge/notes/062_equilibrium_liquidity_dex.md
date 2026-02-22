# Equilibrium Liquidity and Risk Offsetting in Decentralised Markets

**Authors**: Fayçal Drissi, Xuchen Wu, Sebastian Jaimungal
**Date**: 2025
**Topic**: DEX Liquidity, Risk Management, CEX-DEX Interaction

## Summary

The paper develops an economic model of Decentralised Exchanges (DEXs) where risk-averse Liquidity Providers (LPs) manage their inventory risk by hedging on a Centralised Exchange (CEX). The model captures the friction of imperfect replication and trading costs. It finds that rational LPs manage risk primarily by **reducing the liquidity supplied** to the DEX rather than just aggressively hedging. Higher risk aversion or trading costs in the CEX lead to thinner DEX liquidity.

## Key Concepts

1.  **Endogenous Liquidity Supply**:
    - LPs do not just passively supply liquidity; they optimize the quantity based on their ability to hedge.
    - **Viability Condition**: If hedging costs or risk aversion are too high, LPs withdraw, leading to market breakdown.
2.  **Hedging Frictions**:
    - Perfect replication is impossible due to discrete trading and costs on CEX.
    - LPs balance **Inventory Risk** (holding the wrong asset) vs. **Execution Risk/Cost** (hedging on CEX).
3.  **Market Depth & Volatility**:
    - Higher fundamental volatility erodes liquidity depth.
    - Uninformed demand supports depth.

## Implications for Our Platform

- **LP Strategy Design**:
  - **Action**: When designing LP strategies for DEXs (or imitating them), explicitly model the **Cost of Hedging**.
  - **Formula**: `OptimalDepth ~ UninformedVolume / (RiskAversion * Volatility * HedgingCost)`.
  - If CEX spreads widen (higher hedging cost), we must purely widen our DEX spreads or reduce size.
- **Inventory Management**:
  - Don't aim for perfect delta neutrality if trading costs are high. Accepted partial inventory risk is optimal.

## Tags

#DEX #LiquidityProvision #RiskManagement #MarketMicrostructure #CEXDEX
