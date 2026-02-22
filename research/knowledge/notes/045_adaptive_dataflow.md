# History Is Not Enough: An Adaptive Dataflow System for Financial Time-Series Synthesis

**Authors**: Haochong Xia et al. (NTU, SMU)
**Date**: January 2026
**Topic**: Concept Drift, Data Augmentation, Adaptive Pipeline, Reinforcement Learning

## Summary

The paper addresses the **Concept Drift** problem in financial time series (non-stationarity). It proposes an **Adaptive Dataflow System** where a "Planner" (RL Agent) dynamically controls data augmentation parameters (Jitter, MixUp, Time Warping) based on the model's _Validation Loss_ feedback.

## Key Concepts

1.  **Parameterized Augmentation Module (`M`)**:
    - **Single-Stock**: Jittering (Noise), Scaling, Magnitude Warping (Cubic Spline), Permutation.
    - **Multi-Stock**: **MixUp** (Linear combination of two correlated stocks), **Amplitude Mix** (Frequency domain mixing).
    - **Curated Constraints**: Enforces K-line consistency (High >= Low) to maintain realism.
2.  **Adaptive Control**:
    - **Planner**: Learns a policy $\pi(p, \lambda | f, x)$ to output probability $p$ (which op to use) and strength $\lambda$.
    - **Scheduler**: Determines _how much_ data to augment ($\alpha$).
    - **Feedback Loop**: If Validation Loss increases (overfitting), the Planner increases augmentation diversity to force generalization.

## Implications for Our Platform

- **Dynamic Augmentation**: We should not use static data augmentation. We should implement a **"Curriculum Scheduler"**:
  - Start training with clean data.
  - As training progresses, ramp up `Jitter` and `MixUp` intensity.
  - If `Val_Loss` diverges from `Train_Loss`, increase `Magnitude_Warping` to simulate regime shifts.
- **Cointegration MixUp**: When training on multiple assets (e.g. ETH/BTC), generating synthetic data by mixing cointegrated pairs is a mathematically valid way to increase sample size without breaking market logic.

## Tags

#DataAugmentation #ConceptDrift #AdaptiveLearning #TimeSeriesSynthesis #RLPipeline #MixUp
