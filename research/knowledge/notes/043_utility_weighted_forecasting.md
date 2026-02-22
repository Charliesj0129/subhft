# Utility-Weighted Forecasting and Calibration for Risk-Adjusted Decisions under Trading Frictions

**Authors**: Craig Wright
**Date**: January 2026
**Topic**: Forecasting under Frictions, Model Calibration, Utility-Weighted Loss, Turnover Constraints

## Summary

The paper argues that maximizing traditional forecast accuracy (MSE, LogScore) fails to improve trading performance in financial markets with frictions (Spreads, Slippage, Fees) and constraints (Leverage, Turnover). It introduces **Utility-Weighted Calibration (UWC)**, which trains models to be "well-calibrated" specifically in the states that matter most to the downstream portfolio optimizer.

## Key Concepts

1.  **Friction Operator**: Explicitly modeling costs (impact, fees) and constraints (turnover limits, leverage caps) in the decision function.
2.  **Utility-Weighted Calibration Loss**:
    - Instead of minimizing uniform error, minimize: `Loss = Sum( w_it * |Forecast_i - Reality_i| )`
    - Where `w_it` is the **marginal sensitivity** of the constrained optimization problem to the forecast error.
    - **Insight**: An error in a **Constraint-Binding State** (e.g. at Leverage Cap) or a **High-Cost State** (e.g. low liquidity) is exponentially more expensive than an error in a free/cheap state.
3.  **Result**: UWC reduces **Realized Decision Loss** by >30% and improves Sharpe Ratio significantly by reducing the frequency of binding constraints (avoiding "Corner Solution Panics").

## Implications for Our Platform

- **Loss Function Innovation**: For our Deep Learning models (DeepXDE, RL), we should implement a **"Utility-Aware Loss"**.
  - `Loss_t = |y_pred - y_true| * constraint_sensitivity_t`
  - `constraint_sensitivity_t` could be derived from the dual variables (Lagrange multipliers) of our optimization problem (e.g. if we are close to max position size, weight errors highly).
- **Turnover Awareness**: The model should be penalized more for errors that cause _high turnover_ (flip-flopping) than errors that suggest _holding_.
- **Calibration**: We should track "Utility Calibration Error" not just "Brier Score".

## Tags

#Forecasting #ModelCalibration #LossFunction #TradingFrictions #TurnoverConstraints #UtilityWeightedLoss
