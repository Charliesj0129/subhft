# Asymptotic and Finite-Sample Distributions of Empirical Relative Entropy

**Authors**: Matthieu Garcin, Louis Perot
**Date**: December 2025
**Topic**: Change-Point Detection, Relative Entropy, Kullback-Leibler Divergence, Berry-Esseen Bounds

## Summary

This paper provides the theoretical foundation for using **Empirical Relative Entropy** (Kullback-Leibler Divergence) as a statistical test for **Change-Point Detection (CPD)** in time series. It derives **finite-sample concentration inequalities** (Berry-Esseen bounds) for the KL divergence, allowing for robust hypothesis testing even with small sample sizes, which is crucial for detecting sudden market regime shifts.

## Key Concepts

1.  **Empirical Relative Entropy**:
    - $ D\_{KL}(\hat{p}\_n \| \hat{q}\_m) = \sum \hat{p}\_n(i) \log \frac{\hat{p}\_n(i)}{\hat{q}\_m(i)} $.
    - Measures the distance between the empirical distribution of the current window ($\hat{p}_n$) and a reference window ($\hat{q}_m$).
2.  **Change-Point Detection (CPD)**:
    - **Null Hypothesis ($H_0$)**: No change (samples come from same distribution).
    - **Test**: Computed $D_{KL}$. If $D_{KL} > Threshold$, reject $H_0$ -> **Regime Change Detected**.
3.  **Berry-Esseen Bounds**:
    - The paper provides precise bounds for the CDF of the estimator. This allows setting **Confidence Intervals** (e.g., 99%) for the threshold, reducing false positives compared to simple asymptotic approximations (Chi-square).

## Implications for Our Platform

- **Regime Detection Module**:
  - Upgrade our `volatility_regime` logic. Instead of just monitoring variance, monitor the **Full Distribution** of returns or order book imbalances.
  - **Action**: Implement a `RelativeEntropyCPD` class in `analytics`.
  - **Usage**:
    - Window 1 (Reference): Last 1 hour of trade ticks.
    - Window 2 (Test): Last 1 minute of trade ticks.
    - Compute $D_{KL}$. If significant, trigger `Regime_Switch` event (e.g., Normal -> Volatile).
  - **Advantage**: Can detect "silent" risks (e.g., liquidity drying up, tail fattening) _before_ the variance spikes.

## Tags

#ChangePointDetection #RelativeEntropy #KullbackLeibler #Statistics #RegimeSwitching
