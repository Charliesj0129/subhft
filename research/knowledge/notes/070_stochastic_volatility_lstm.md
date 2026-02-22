# Stochastic Volatility Modelling with LSTM Networks: A Hybrid Approach

**Authors**: Anna Perekhodko, Robert Ślepaczuk
**Date**: 2025
**Topic**: Volatility Forecasting, Hybrid Models, Stochastic Volatility, Neural Networks

## Summary

The paper proposes a hybrid volatility forecasting model combining **Stochastic Volatility (SV)** models and **Long Short-Term Memory (LSTM)** networks. The SV model (based on latent processes) generates one-step-ahead forecasts of _latent volatility_, which are then fed as additional features into an LSTM trained on raw returns and historical volatility. This hybrid approach significantly outperforms standalone SV or LSTM models in forecasting S&P 500 volatility (2000-2025).

## Key Concepts

1.  **Hybrid Architecture**:
    - **SV Model**: Provides a "statistically grounded" baseline estimate of volatility `exp(h_t)`. Handles noise/jumps well.
    - **LSTM**: Captures non-linear dependencies and long-term memory that SV misses.
    - **Integration**: Feed the SV forecast `sigma_hat_SV` into the LSTM input vector `x_t = [returns, hist_vol, sigma_hat_SV]`.
2.  **Performance**:
    - Hybrid model reduces Mean Absolute Percentage Error (MAPE) and Mean Squared Error (MSE) compared to baselines.

## Implications for Our Platform

- **Volatility Forecasting**:
  - **Action**: Don't rely solely on GARCH or solely on deep learning. Combine them.
  - **Feature Engineering**:
    - Fit a GARCH(1,1) or SV model online.
    - Use its _forecast_ as an input feature for our main RL agent or Price Prediction model.
    - This provides a robust "prior" regarding current volatility regime.

## Tags

#VolatilityForecasting #HybridModels #StochasticVolatility #LSTM #DeepLearning
