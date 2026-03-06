# A High Frequency Trade Execution Model for Supervised Learning

ref: 128
arxiv: https://arxiv.org/abs/1710.03870v3
Authors: Matthew F Dixon
Published: 2017-10-11T00:57:26Z

## Abstract
This paper introduces a high frequency trade execution model to evaluate the economic impact of supervised machine learners. Extending the concept of a confusion matrix, we present a 'trade information matrix' to attribute the expected profit and loss of the high frequency strategy under execution constraints, such as fill probabilities and position dependent trade rules, to correct and incorrect predictions. We apply the trade execution model and trade information matrix to Level II E-mini S&P 500 futures history and demonstrate an estimation approach for measuring the sensitivity of the P&L to the error of a Recurrent Neural Network. Our approach directly evaluates the performance sensitivity of a market making strategy to prediction error and augments traditional market simulation based testing.

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
- Suggested alpha_id: `high_frequency_trade_execution_model_supervised`
- Scaffold: `python -m research scaffold high_frequency_trade_execution_model_supervised --paper 128`
- Bridge flow: `python -m research paper-to-prototype 128 --alpha-id high_frequency_trade_execution_model_supervised`
