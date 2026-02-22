# The Fair Basis: Funding and Capital in the Reduced Form Framework

**Authors**: Wujiang Lou
**Date**: 2020 (Updated)
**Topic**: Credit Arbitrage, Negative Basis, Economic Capital, KVA

## Summary

The paper analyzes the economics of **Negative Basis Trades** (long bond + buy CDS protection). While theoretically a risk-free arbitrage, the 2008 crisis exposed significant risks. The author argues that the "Basis" is not just a liquidity premium but a compensation for **Funding Costs** and **Economic Capital** (regulatory capital charges) required to warehouse the unhedgeable "Jump-to-Default" risk.

## Key Concepts

1.  **Hedging Error**:
    - Even with CDS, you are not perfectly hedged.
    - **Jump-to-Default (JtD) Risk**: If the issuer defaults, the Bond recovery and CDS payout might not match perfectly or settle instantly.
    - **Gap Risk**: The risk that the bond price moves significantly while the CDS spread doesn't (basis widening).
2.  **Economic Capital (EC)**:
    - Banks must set aside capital (VaR-based) to cover potential losses from this hedging error.
    - This capital has a cost (Dividend/ROE requirement).
3.  **Fair Basis Formula**:
    - $ Basis\_{fair} \approx (FundingCost - RiskFree) + (CostOfCapital \times CapitalRequired) $.
    - This explains why the basis persists: it's the "price" of the balance sheet usage.

## Implications for Our Platform

- **Risk Pricing Integration**:
  - **Action**: Update our `OpportunityCost` calculator.
  - For any "Arbitrage" strategy (Basis, Carry), we must explicitly deduct **Capital Charges**.
  - Formula: `ExpectedReturn -= (CapitalAllocation * TargetROE)`.
  - If we ignore this, we will overallocate to capital-intensive "low risk" trades that actually destroy shareholder value (ROIC < Cost of Capital).
- **Negative Basis Strategy**:
  - Only enter negative basis trades when the spread is _wider_ than this "Fair Basis" floor.

## Tags

#CreditArbitrage #BasisTrading #EconomicCapital #KVA #RiskManagement
