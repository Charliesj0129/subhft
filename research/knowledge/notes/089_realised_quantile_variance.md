# Realised Quantile-Based Estimation of the Integrated Variance

**Authors**: Kim Christensen, Roel Oomen, Mark Podolskij
**Date**: 2010 (Accessed 2026)
**Topic**: Volatility Estimation, Quantile Realised Variance, Jump Robustness, Outlier Robustness

## Summary

The paper proposes **Quantile-Based Realised Variance (QRV)**, a new estimator for Integrated Variance (IV) that is robust to both **Finite Activity Jumps** and **Outliers**.

- **Methodology**:
  - Split high-frequency data into $n$ blocks of $m$ returns.
  - In each block, compute the $\lambda$-th quantile (e.g., 95%) of the returns.
  - For Normal dist, $Q_{0.95} \approx 1.645 \sigma$. Invert this relation to estimate local volatility $\hat{\sigma}^2_i$.
  - Average these local indices to get total Integrated Variance.
- **Key Findings**:
  - **Jump Robustness**: Prior estimators like Bi-Power Variation (BPV) are robust to jumps but sensitive to outliers. QRV is robust to both because quantiles naturally ignore tails ($< 100\%$).
  - **Efficiency**: By using a weighted combination of multiple quantiles, QRV approaches maximum likelihood efficiency (parametric bound).
  - **Subsampling**: A valid subsampling scheme improves efficiency further (rate $N^{-1/2}$ without noise, $N^{-1/4}$ with noise).

## Key Concepts

1.  **QRV Estimator**:
    - $QRV = \frac{m}{N} \sum_{i=1}^n \frac{q_i(\lambda)^2}{\nu_{\lambda}}$.
    - Relies on the fact that for a diffusion $dX = \sigma dW$, increments are locally normal.
2.  **Outlier Resistance**:
    - Standard RV ($\sum r^2$) explodes with one bad tick (outlier).
    - QRV uses, say, the 90th percentile. A single 100-sigma outlier doesn't move the 90th percentile at all.
3.  **Jump Robustness**:
    - Jumps are "large" deviations. As long as the number of jumps in a block is less than $(1-\lambda)m$, the quantile estimator is unaffected (the jump ends up in the discarded tail).

## Implications for Our Platform

- **Data Cleaning**:
  - **Action**: Instead of complex pre-filtering for outliers, compute volatility using **QRV** directly. It essentially "auto-cleans" data.
- **Volatility Signal**:
  - **Feature**: Add `alpha_qrv_vol` to our feature set. It's a cleaner signal of "true" diffusive volatility, stripping out jumps (which are mean-reverting or one-off) and bad ticks.
  - **Param**: Use $\lambda \approx 0.90-0.95$. Block size $m \approx 50-100$ ticks.

## Tags

#VolatilityEstimation #RealisedVariance #Quantiles #RobustStatistics #Jumps #HighFrequencyData
