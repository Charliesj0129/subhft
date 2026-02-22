# Financial Vision Based Reinforcement Learning Trading Strategy

**Authors**: Yun-Cheng Tsai, Fu-Min Szu, Jun-Hao Chen, Samuel Yen-Chi Chen
**Date**: 2022-02
**Topic**: Financial Vision, Reinforcement Learning, PPO, GAF, Candlestick Patterns, Explainable AI

## Summary

The paper proposes a **Financial Vision** approach to RL trading, converting time series data into images to leverage CNNs.

- **Methodology**:
  1.  **GAF Encoding**: Uses **Gramian Angular Field (GAF)** to encode time series (OHLC) into images, preserving temporal correlation in a 2D matrix format suitable for CNNs.
  2.  **Pattern Recognition**: A pre-trained CNN identiﬁes 8 classic Candlestick Patterns (Morning Star, Engulfing, etc.) from the GAF images.
  3.  **RL Agent (PPO)**: A Proximal Policy Optimization agent takes the _Probability Distribution of Patterns_ (from the CNN) as the state input, rather than raw prices.
- **Key Idea**:
  - Separate **Perception** (CNN seeing the pattern) from **Action** (RL deciding what to do with it).
  - This improves **Explainability**: "The agent bought because it saw a Bullish Engulfing pattern with 85% confidence."
- **Results**:
  - Transfer Learning: Trained on Ethereum (15min), tested on US ETFs (SPY, BNO, EWJ).
  - Claimed robustness during COVID-19 crash.

## Key Concepts

1.  **Financial Vision**:
    - Treating chart constraints not as statistical series but as **Visual Patterns**.
    - GAF Encoding: $G_{i,j} = \cos(\phi_i + \phi_j)$. This maps correlation between time steps $i$ and $j$ to a pixel.
2.  **Explainable RL**:
    - Instead of an End-to-End Black Box (Price $\to$ Action), use a "Glass Box" features (Price $\to$ Pattern $\to$ Action).

## Implications for Our Platform

- **State Representation**:
  - **GAF for CNNs**: If we use CNN-based RL, GAF is a standard technique to avoid 1D Conv limitations. It captures long-range dependencies in a 2D grid.
  - **Pattern Features**: We can add a "Pattern Recognition" module (Rule-based or CNN) that outputs one-hot encodings of classic patterns (e.g., "Hammer", "Doji") as explicit features for the RL agent. This might speed up convergence compared to forcing the agent to learn these geometric patterns from raw OHLC.
- **Transfer Learning**:
  - The paper suggests crypto-trained patterns transfer well to equities. We could pre-train our pattern recognizers on high-frequency crypto data before applying them to lower-frequency futures data.

## Tags

#ComputerVision #ReinforcementLearning #PPO #GAF #CandlestickPatterns #ExplainableAI
