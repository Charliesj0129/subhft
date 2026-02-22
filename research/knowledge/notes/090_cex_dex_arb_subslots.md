# Second Thoughts: How 1-second subslots transform CEX-DEX Arb

**Authors**: Aleksei Adadurov, Sergey Barseghyan, Anton Chtepine, Antero Eloranta, Andrei Sebyakin, Arsenii Valitov
**Date**: 2026-01-02
**Topic**: CEX-DEX Arbitrage, Execution Risk, Ethereum, Subslots, Preconfirmations

## Summary

The paper investigates the impact of reducing Ethereum block/slot times (e.g., via subslots or preconfirmations) from 12 seconds to 1 second on **CEX-DEX arbitrage**.

- **Problem**: In the current 12s regime, a CEX-DEX arbitrageur faces significant "Execution Risk". The CEX leg is instant, but the DEX leg takes up to 12s to confirm. During this time, prices move, and the trade might fail or become unprofitable.
- **Model**:
  - **Agent**: A risk-averse arbitrageur who explicitly models the probability of DEX leg failure and the cost of unwinding the CEX leg.
  - **Decision**: Enter only if $E[\text{Profit}] - \lambda \sqrt{\text{Var}[\text{Profit}]} > 0$.
  - **Simulation**: Calibrated to Binance vs Uniswap V3 (ETH-USDC).
- **Key Findings**:
  - **Volume Explosion**: Reducing slot time to 1s increases arbitrage transaction count by **~535%** and volume by **~203%**.
  - **Risk Reduction**: Faster slots reduce the variance of the "unwind" cost (execution risk), making marginally profitable arbitrages viable, especially in low-fee pools.

## Key Concepts

1.  **Execution Risk in Arb**:
    - Arb is not risk-free. If DEX leg fails (revert, uncled, front-run), you are left with a naked CEX position.
    - **Fallback Logic**: If DEX fails, do you close immediately on CEX (take loss) or retry?
2.  **Subslots/Preconfs**:
    - Mechanisms that give faster "pre-confirmation" effectively reduce the latency of the DEX leg to ~1s. This tightens the loop and aligns CEX/DEX prices much faster.

## Implications for Our Platform

- **Arbitrage Strategy**:
  - **Action**: If the platform trades CEX-DEX arb, we must account for _block time_ in our risk model.
  - **Alpha**: As Ethereum moves towards faster finality (or if we trade on L2s/Solana), the "minimum viable spread" decreases. We should update our thresholds dynamically based on the chain's block time.
- **Simulation**:
  - **Feature**: Our backtester must simulate "Trade Failure" and "Unwind Cost". Just assuming the DEX leg lands is unrealistic and overestimates PnL.

## Tags

#CEXDEXArbitrage #MEV #Ethereum #MarketMicrostructure #ExecutionRisk #Subslots
