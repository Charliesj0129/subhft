# An End-to-End Data-Driven Optimisation Framework

**Authors**: Florent Dewez et al. (Inria, UCL)
**Date**: February 2021
**Topic**: Trajectory Optimization, Data-Driven Control, Constraints

## Summary

The paper proposes a general framework for optimizing dynamic trajectories (e.g., aircraft, robots, or **trading execution**) under constraints, using a **data-driven approach**. Rather than relying on potentially unknown or complex governing differential equations, it optimizes trajectories by minimizing a distance to a set of **reference trajectories** (successful historical examples) using a Maximum A Posteriori (MAP) approach. It introduces `PyRotor` (Python library).

## Key Concepts

1.  **Reference Trajectories**:
    - Instead of solving $ \dot{x} = f(x, u) $ which we might not know, collect successful historical trajectories $\{y_{R}^{i}\}$.
    - Model these as noisy observations of an "optimal" trajectory $y^* = c^* + \epsilon$.
2.  **Basis Function Decomposition**:
    - Project trajectories onto a basis (e.g., Fourier, Legendre polynomials) to reduce dimensionality.
3.  **Quadratic Cost Optimization**:
    - Minimize Cost $J(c)$ + Penalty $\kappa \sum_{i} \omega_i ||c - c_{R}^{i}||_{\Sigma^{-1}}^2$.
    - The penalty term keeps the solution close to the "safe" cluster of historical successes (measured by Mahalanobis distance).
4.  **Implicit Constraints**:
    - The covariance matrix $\Sigma$ captures implicit constraints and correlations between variables without explicitly modeling them.

## Implications for Our Platform

- **Execution Algorithms**:
  - Instead of traditional VWAP/TWAP, we can use this **Similarity-Based Execution**.
  - Find the K-Nearest Neighbors of historical execution profiles (volume/price curves) that resulted in low impact/slippage.
  - Optimize the current order schedule to minimize slippage but stay within the "cluster" of these successful profiles.
  - **Action**: Create a `trajectory_optimizer` using `scipy.optimize` (or implement the simple quadratic solver) for `Large Block Execution`.

## Tags

#Execution #TrajectoryOptimization #DataDrivenControl #OptimalControl #PyRotor
