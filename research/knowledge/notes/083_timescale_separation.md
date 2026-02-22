# Fast Times, Slow Times: Timescale Separation in Financial Timeseries Data

**Authors**: Jan Rosenzweig
**Date**: 2026
**Topic**: Signal Processing, Timescale Separation, Stationarity, tICA

## Summary

The paper proposes a method to separate financial time series into **Fast** (noise/arbitrage) and **Slow** (trend/risk) components using **Generalized Eigenvalue Problems**.

- **Methodology**:
  1.  **Variance Timescales (Linear tICA)**: Minimized drift of variance $\to$ Eigenvalues correspond to autocorrelation decay times.
  2.  **Tail Timescales (Nonlinear tICA)**: Minimizes drift of higher-order moments (Kurtosis) to find "Tail-Stationary" components.
- **Key Findings**:
  - Financial data spans milliseconds (HFT) to years (Macro).
  - Standard PCA mixes these scales. **tICA (Time-lagged Independent Component Analysis)** separates them.
  - The "Slow" components found in-sample (e.g., 2006-2010) tend to remain slow out-of-sample (2010-2025), even if their composition changes.

## Key Concepts

1.  **tICA vs PCA**:
    - PCA maximizes variance. tICA maximizes **autocorrelation** (slowness).
    - Result: PCA finds the "Loudest" signals. tICA finds the "Most Persistent" signals.
2.  **Generalized Eigenvalue Problem**:
    - $C(\tau) w = \lambda C(0) w$.
    - Solves for weights $w$ such that the signal $w^T X$ has max autocorrelation at lag $\tau$.
3.  **Tail Stationarity**:
    - By optimizing higher moments ($k=4$), we can find portfolios that have stable tail risks, even if their variance drifts.

## Implications for Our Platform

- **Alpha Blending**:
  - **Action**: Use **tICA** instead of PCA to robustify our alpha factors. We want alphas that are _persistent_ (slow decay), not just volatile.
  - **Signal**: Extract the "Fastest" components (smallest eigenvalues) to identify **mean-reversion** opportunities (HFT noise). Extract "Slowest" for **trend following**.
- **Risk Management**:
  - **Tail Risk**: Use the Nonlinear tICA to construct a "Tail Hedging" portfolio that is independent of the main market tail risks.

## Tags

#SignalProcessing #tICA #Stationarity #TimescaleSeparation #AlphaConstruction
