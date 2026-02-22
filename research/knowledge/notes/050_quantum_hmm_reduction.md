# Quantum Dimension Reduction of Hidden Markov Models

**Authors**: Rishi Sundar & Thomas J. Elliott (University of Manchester)
**Date**: January 2026
**Topic**: Hidden Markov Models (HMMs), Quantum Dimension Reduction, Quantum Memory

## Summary

The paper proposes a **Quantum Hidden Markov Model (QHMM)** compression technique using **Dilation** and **Matrix Product States (iMPS)**. It maps probabilistic classical processes (HMMs) to a quantum memory representation and uses Variational Truncation (iMPS) to create a lower-dimensional ($C_q$) compressed model.

## Key Concepts

1.  **Dilation**:
    - Classical HMMs are often non-deterministic (non-unifilar).
    - Dilating the alphabet ($X \rightarrow X \times Y$) makes the HMM deterministic (Unifilar).
    - **Result**: Deterministic HMMs can be represented by a **Normal iMPS** (Primitive Transfer Operator).
2.  **Quantum Compression**:
    - Truncate the **Bond Dimension** of the iMPS.
    - **Result**: A compressed quantum generator that reproduces the statistics of the original huge HMM with much fewer states.
    - **Advantage**: Quantum Statistical Memory $C_q$ is strictly lower than Classical Memory $C_\mu$.

## Implications for Our Platform

- **Regime Modeling**: If we model latent market states (HMM) using **Tensor Networks (MPS)**, we can compress huge models (e.g. 1000 states) into very small bond dimensions (e.g. 10) for efficient simulation.
- **Quantum Advantage**: This is a direct application of "Quantum-Inspired" methods (Tensor Networks) to classical stochastic modeling. It suggests that Tensor Networks are valid, efficient representations of market dynamics.

## Tags

#QuantumHMM #MatrixProductStates #TensorNetworks #StochasticModeling #DimensionReduction #MarketRegimes
