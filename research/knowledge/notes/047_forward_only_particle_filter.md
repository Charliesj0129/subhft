# A forward-only scheme for online learning of proposal distributions in particle filters

**Authors**: Procope-Mamert et al. (INRAE, ENSAE, Institut Polytechnique de Paris)
**Date**: January 2026
**Topic**: Particle Filters, Sequential Monte Carlo (SMC), Proposal Distributions, Online Learning

## Summary

The paper introduces a **Forward-Only Online Learning** scheme for constructing **Particle Filter Proposal Distributions** (SMC). Unlike unstable Backward Smoothing methods (e.g. Iterated Auxiliary PF), this forward scheme gradually learns an optimal proposal distribution that minimizes the variance of the marginal likelihood estimator.

## Key Concepts

1.  **State-Space Models (SSM)**: Sequential latent variable models (e.g. Regime-Switching HMM, Volatility Models).
2.  **Particle Filter Degeneracy**: Standard Bootstrap Particle Filters suffer from "weight collapse" where one particle dominates due to poor proposal distributions.
3.  **Forward-Only Iterative Scheme**:
    - Iteratively refines the proposal kernel $M_t(x_{t-1}, dx_t)$ using a Variance-Minimizing objective function.
    - **Result**: More robust and stable than backward methods, especially for complex non-linear models.
    - **Convergence**: Converges to the optimal proposal (the full smoothing distribution marginal) over iterations.

## Implications for Our Platform

- **Regime Tracking**: If we implement a **Particle Filter** to track Hidden Market States (e.g. "Whale Accumulating", "Retail FOMO"), we should use this **Forward-Only** method to update our proposal distribution online.
- **Volatility Estimation**: For Stochastic Volatility models (Heston, etc.) running in real-time, this method provides a stable way to estimate latent volatility without re-running backward smoothing over the entire history.
- **Robustness**: Prefer this over complex MCMC methods for real-time applications where data arrives sequentially.

## Tags

#ParticleFilter #SequentialMonteCarlo #StateSpaceModels #OnlineLearning #BayesianInference #RobustFiltering
