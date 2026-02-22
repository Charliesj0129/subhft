# Design of a Decentralized Fixed-Income Lending AMM (BondMM-A)

**Authors**: Tianyi Ma (Shanghai Jiao Tong)
**Date**: December 2025
**Topic**: DeFi, Automated Market Makers (AMM), Fixed Income, Bonds

## Summary

This paper proposes **BondMM-A**, a decentralized AMM protocol for trading Fixed-Income bonds with **Arbitrary Maturities**. Unlike existing protocols (Yield, Notional) that require separate liquidity pools for each maturity date, BondMM-A uses a unified pool and a **Present Value Invariant** to trade bonds of any $T$.

## Key Concepts

1.  **Present Value Invariant**:
    - Instead of fixing the "Face Value" of bonds in the pool, it fixes the "Present Value".
    - Invariant: $ X = x e^{-rt} $.
2.  **Unified Liquidity**:
    - LPs deposit into a single pool.
    - Traders can mint/redeem bonds of _any_ maturity $t$. The pricing adjusts automatically based on the pool's utilization and the requested maturity.

## Implications for Our Platform

- **Low Priority**: Our current focus is Centralized HFT.
- **Future Potential**:
  - If we expand to **DeFi Arbitrage**, this is a key protocol to watch.
  - Arbitrage Opportunity: Rate differentials between `BondMM-A` (DeFi) and Centralized Futures (Basis).
  - **Action**: Archive for "DeFi Expansion" phase. No immediate code required.

## Tags

#DeFi #AMM #FixedIncome #SmartContracts #BondPricing
