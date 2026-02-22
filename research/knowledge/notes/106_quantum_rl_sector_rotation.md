# Quantum Reinforcement Learning Trading Agent for Sector Rotation in the Taiwan Stock Market

**Authors**: Chi-Sheng Chen, Xinyu Zhang, Ya-Chuan Chen
**Date**: 2025-10
**Topic**: Quantum Reinforcement Learning (QRL), Sector Rotation, PPO, Quantum Neural Networks (QNN)

## Summary
The paper evaluates a **Hybrid Quantum-Classical Reinforcement Learning** framework for **Sector Rotation** in the Taiwan stock market. It benchmarks classical PPO agents (LSTM, Transformer) against quantum-enhanced variants (QNN, QRWKV, QASA).
*   **Methodology**:
    *   **Task**: Select the top-10 performing industry sectors (out of 47) for the next day.
    *   **Action Space**: Portfolio allocation across sectors.
    *   **State Space**: Technical indicators (MA, Momentum, Volatility) from sector indices.
    *   **Models**:
        *   **Classical**: LSTM, Transformer.
        *   **Quantum**:
            *   **QNN**: Variational Quantum Circuit (VQC) replacing a dense layer.
            *   **QRWKV**: Quantum variant of the RWKV (Receptance Weighted Key Value) model.
            *   **QASA**: Quantum Attention Self-Attention (replacing temporal attention with VQC).
*   **Key Findings**:
    *   **The Alignment Gap**: Quantum models achieved **higher training rewards** (better at predicting the specific "Top-10" binary target) but **lower real-world returns** (Sharpe Ratio, Cumulative Return) compared to classical models.
    *   **Overfitting**: The high expressivity of Quantum Circuits allowed them to "game" the proxy reward function (binary prediction) without learning robust long-term value generation.
    *   **Classical Wins**: LSTM and Transformer baselines outperformed all quantum variants in actual backtesting.

## Key Concepts
1.  **Quantum Policy Networks**:
    *   Replacing classical layers with VQCs (Variational Quantum Circuits).
    *   **Angle Embedding**: Encoding classical data $x$ into quantum states $|\psi(x)\rangle$.
    *   **Measurement**: Converting quantum state back to classical vector via expectation values of Pauli-Z operators.
2.  **Proxy Reward Misalignment**:
    *   Designing a reward function (e.g., $+1$ if in Top 10, $-0.1$ otherwise) creates a proxy objective. Quantum models, being powerful function approximators, overfit this proxy, diverging from the true goal (Risk-Adjusted Returns).

## Implications for Our Platform
-   **Skepticism on Quantum**:
    *   Currently, Quantum RL (NISQ era) does **not** provide an edge over classical methods for this type of problem. It adds complexity and instability without performance gains. We should stick to **Transformer/LSTM** backbones for now.
-   **Reward Function Engineering**:
    *   The paper highlights a critical pitfall: **Don't use binary proxy rewards**. If we train our RL agent, the reward *must* be the actual Sharpe Ratio or PnL, not a classification accuracy proxy (e.g., "did price go up?"). Models will game the proxy and lose money.

## Tags
#QuantumRL #QML #SectorRotation #PPO #Transformer #RewardShaping #Overfitting
