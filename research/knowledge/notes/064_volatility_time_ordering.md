# Impact of Volatility on Time-Based Transaction Ordering Policies

**Authors**: Ko Sunghun, Jinsuk Park
**Date**: 2025
**Topic**: MEV, Time-Based Ordering, Express Lane Auction (ELA)

## Summary

The paper studies Arbitrum's **Express Lane Auction (ELA)**, where bidders compete for 1-minute exclusive priority access. This access typically captures MEV from CEX-DEX arbitrage. The authors find that bidders consistently **underbid** relative to the risk-neutral expected value of the MEV. This discount is attributed to **Variance Risk Premium (VRP)**: difficulty in forecasting short-term volatility and risk aversion.

## Key Concepts

1.  **Timeboost / Express Lane**:
    - Winners get a latency advantage (e.g., 200ms) over normal transactions.
    - This is enough to capture most CEX-DEX arbitrage opportunities.
2.  **Valuation Discount**:
    - Value depends on expected integrated variance (`L * sqrt(P) * IV`).
    - Bidders bid less than expected profit (`bid < E[Profit]`).
    - Discount increases with higher uncertainty (`Var(IV)`).
3.  **Risk Aversion**:
    - Bidders are risk-averse and demand a premium for holding the volatility risk.

## Implications for Our Platform

- **Auction Bidding Strategy**:
  - **Action**: If participating in block auctions (like Flashbots or Timeboost), incorporate a **Risk Premium**.
  - **Discount**: Bid roughly `E[MEV] - RiskFactor * Var(MEV)`.
  - This prevents overbidding and winning the "Winner's Curse".
- **Volatility Forecasting**:
  - Accurate short-term volatility forecasting is crucial for correct valuation.
  - Better volatility models = More accurate bids = Higher expected profit.

## Tags

#MEV #Blockchain #AuctionTheory #Volatility #Arbitrage #RiskPremia
