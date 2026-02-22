# A unified theory of order flow, market impact, and volatility

**Authors**: Johannes Muhle-Karbe, Youssef Ouazzani Chahdi, Mathieu Rosenbaum, Grégoire Szymanski
**Date**: 2026
**Topic**: Order Flow, Market Impact, Rough Volatility, Hawkes Processes, Microstructure

## Summary

The paper proposes a unified microstructural model decomposing order flow into **Core Flow** (autonomous, long-memory, metaorders) and **Reaction Flow** (endogenous, HFT/MM response). Both are modeled as Hawkes processes. The key finding is that a single parameter $H_0$ (persistence of core flow) governs the scaling limits of:

1.  **Signed Order Flow**: Converges to a "Mixed Fractional Brownian Motion" (Standard Brownian Motion + Fractional Brownian Motion with Hurst $H_0$). $H_0 \approx 0.75$.
2.  **Unsigned Volume**: Converges to a Rough process with Hurst $H_0 - 1/2 \approx 0.25$.
3.  **Volatility**: Converges to a Rough Volatility model with Hurst $2H_0 - 3/2 \approx 0.0$.
4.  **Market Impact**: Follows a power law $t^{-(1-\alpha)}$ which implies square-root impact when $\alpha \approx 1/2$.

## Key Concepts

1.  **Two-Layer Hawkes**:
    - **Core**: Persistent, splits large orders. Driving force.
    - **Reaction**: Responses to core (liquidity provision).
2.  **Roughness & Persistence**:
    - Signed order flow looks diffusive ($H \approx 0.5$) at high frequency but persistent ($H \approx 0.75$) at low frequency.
    - Volume is "rough" ($H \approx 0.25$).
    - Volatility is "very rough" ($H \approx 0.05-0.1$).
    - The model unifies these disparate estimates into a single framework.
3.  **Scale Dependence**:
    - Estimating Hurst on signed flow varies by timeframe (Fig 3).
    - Accounting for the "Mixed" nature enables stable estimation of the true core persistence $H_0$.

## Implications for Our Platform

- **Volatility Forecasting**:
  - **Action**: Use the derived relationship $H_{vol} = 2H_{flow} - 1.5$. If we estimate order flow persistence $H_{flow}$ accurately, we can better parameterize our rough volatility models.
- **Impact Modeling**:
  - **Action**: Use the "Square Root Law" as a baseline. The paper confirms this is consistent with rough volatility.
  - **Calibration**: Calibrate $H_0$ from low-frequency order flow (hourly) rather than high-frequency (tick) to avoid the "reaction flow" noise.
- **Order Flow Simulation**:
  - To simulate realistic markets, do not use simple Brownian Motion. Use a **Mixed Fractional** process: $X_t = B_t + W^H_t$, where $B_t$ is brownian (reaction) and $W^H_t$ is fractional (core).

## Tags

#OrderFlow #MarketImpact #RoughVolatility #HawkesProcesses #MarketMicrostructure #MathFinance
