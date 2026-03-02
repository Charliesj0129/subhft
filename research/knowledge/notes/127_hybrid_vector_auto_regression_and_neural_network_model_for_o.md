# Hybrid Vector Auto Regression and Neural Network Model for Order Flow Imbalance Prediction in High Frequency Trading

ref: 127
arxiv: https://arxiv.org/abs/2411.08382v1
Authors: Abdul Rahman, Neelesh Upadhye
Published: 2024-11-13T07:06:22Z

## Abstract
In high frequency trading, accurate prediction of Order Flow Imbalance (OFI) is crucial for understanding market dynamics and maintaining liquidity. This paper introduces a hybrid predictive model that combines Vector Auto Regression (VAR) with a simple feedforward neural network (FNN) to forecast OFI and assess trading intensity. The VAR component captures linear dependencies, while residuals are fed into the FNN to model non-linear patterns, enabling a comprehensive approach to OFI prediction. Additionally, the model calculates the intensity on the Buy or Sell side, providing insights into which side holds greater trading pressure. These insights facilitate the development of trading strategies by identifying periods of high buy or sell intensity. Using both synthetic and real trading data from Binance, we demonstrate that the hybrid model offers significant improvements in predictive accuracy and enhances strategic decision-making based on OFI dynamics. Furthermore, we compare the hybrid models performance with standalone FNN and VAR models, showing that the hybrid approach achieves superior forecasting accuracy across both synthetic and real datasets, making it the most effective model for OFI prediction in high frequency trading.

## Hypothesis
- Signed order-flow imbalance predicts short-horizon price pressure, especially when queue imbalance aligns with the OFI direction.

## Candidate Formula
- `alpha_t = zscore(ofi_l1_ema8_t) * sign(depth_imbalance_ema8_ppm_t)`

## Relevant Features (lob_shared_v1)
- `ofi_l1_raw`
- `ofi_l1_cum`
- `ofi_l1_ema8`
- `depth_imbalance_ppm`
- `depth_imbalance_ema8_ppm`
- `l1_bid_qty`
- `l1_ask_qty`
- `spread_scaled`
- `spread_ema8_scaled`
- `mid_price_x2`
- `microprice_x2`

## Implementation Notes
- Suggested alpha_id: `hybrid_vector_auto_regression_neural_network_mod`
- Scaffold: `python -m research scaffold hybrid_vector_auto_regression_neural_network_mod --paper 127`
- Bridge flow: `python -m research paper-to-prototype 127 --alpha-id hybrid_vector_auto_regression_neural_network_mod`
