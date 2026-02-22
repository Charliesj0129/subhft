# Defining, Estimating and Using Credit Term Structures (Part 3)

**Authors**: Arthur M. Berd, Roy Mashal, Peili Wang (Lehman Brothers)
**Date**: November 2004 (Classic)
**Topic**: Credit Arbitrage, CDS-Bond Basis, Relative Value

## Summary

This paper, part of a classic series, introduces the **Bond-Implied CDS (BCDS)** term structure. It argues that traditional measures like Z-spread or Libor OAS are flawed for comparing bonds to CDS because they don't correctly account for the survival-based valuation of distressed bonds. The paper defines the **CDS-Bond Basis** as the difference between the market CDS spread and the BCDS spread, providing a consistent measure for relative value trading.

## Key Concepts

1.  **Bond-Implied CDS (BCDS)**: A synthetic CDS spread derived from the survival probabilities fitted to cash bond prices.
    - Formula involves substituting the bond-derived survival curve into the par CDS equation.
2.  **CDS-Bond Basis**: `Basis = Market_CDS_Spread - BCDS_Spread`.
    - **Positive Basis**: Market CDS > BCDS. Strategy: Sell Protection (Short CDS), Buy Bond.
    - **Negative Basis**: Market CDS < BCDS. Strategy: Buy Protection (Long CDS), Sell Bond.
3.  **Hedging Strategies**:
    - **Forward CDS Hedge**: Theoretically perfect hedge using a sequence of forward CDS to match the bond's forward price profile.
    - **Spot CDS Hedge**: Practical approximation using spot CDS, often requiring a "staggered" hedge (e.g., 90% long-term, 10% short-term) to manage interest rate and curve risk.

## Implications for Our Platform

- **Credit Arb Module**:
  - We need a `bcds_engine` that fits a hazard rate curve to bond prices (using our existing `yield_curve_fitter`) and calculates the "fair" CDS spread.
  - **Signal**: Monitor the `cds_bond_basis`. If `|basis| > threshold` (e.g., 20bps) and `z_score > 2`, trigger a trade.
- **Risk Management**:
  - Implement the "Staggered Hedge" logic for our credit desk. Don't just hedge duration; hedge the **Forward Price Profile**.

## Tags

#CreditArbitrage #CDS #BasisTrading #FixedIncome #QuantitativeFinance
