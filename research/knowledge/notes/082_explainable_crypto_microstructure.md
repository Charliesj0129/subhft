# Explainable Patterns in Cryptocurrency Microstructure

**Authors**: Bartosz Bieganowski, Robert Ślepaczuk
**Date**: 2026
**Topic**: Market Microstructure, Cryptocurrency, Explainable AI (SHAP), Flash Crash

## Summary

The paper demonstrates that **microstructure patterns are universal** across cryptocurrencies of different market caps (BTC to ROSE).

- **Methodology**:
  - **Data**: Binance Futures tick data (Jan 2022 - Oct 2025).
  - **Model**: CatBoost trained on features like Top-of-Book Imbalance, Spread, VWAP deviations.
  - **Objective**: **GMADL** (Generalized Mean-Absolute Directional Loss) - a direction-aware loss function, superior to MSE for trading.
  - **Explainability**: Uses **SHAP** values to show that feature importance and dependence shapes are stable across assets.
- **Key Findings**:
  - **Universality**: The same features (Imbalance, Spread, VWAP-Mid) drive returns for BTC ($1T cap) and ROSE ($100M cap).
  - **Flash Crash Robustness (Oct 10, 2025)**:
    - **Taker Strategy**: Profited immensely during the crash (exploiting directional moves).
    - **Maker Strategy**: Suffered heavy losses due to **Adverse Selection** (getting run over by toxic flow).

## Key Concepts

1.  **Universal Feature Library**:
    - Features like `Order Flow Imbalance` and `Spread` work everywhere if normalized properly (e.g., relative to price/volatility). No need for asset-specific engineering.
2.  **GMADL Loss**:
    - $\ell = -(\frac{1}{1+e^{-a R \hat{R}}} - 0.5) |R|^b$.
    - Penalizes sign errors heavily, especially on large moves. Weights small noise less.
3.  **Adverse Selection in Crashes**:
    - The paper empirically proves that Makers die in crashes while Takers thrive. Strategies must switch roles or hedge dynamically during stress.

## Implications for Our Platform

- **Signal Generation**:
  - **Action**: Switch our RL reward function or supervised learning loss to **GMADL**. It aligns better with PnL than MSE.
  - **Feature Sets**: We can use a unified feature set for all our crypto assets (BTC, ETH, SOL, etc.).
- **Risk Management**:
  - **Crash Mode**: Detect "Flash Crash" regimes (high volatility + one-sided flow). In these modes, **DISABLE MARKET MAKING** and switch to **TAKER ONLY** or **LIQUIDITY SWEEP** strategies to avoid adverse selection.

## Tags

#CryptoMicrostructure #SHAP #ExplainableAI #FlashCrash #GMADL #CatBoost #AdverseSelection
