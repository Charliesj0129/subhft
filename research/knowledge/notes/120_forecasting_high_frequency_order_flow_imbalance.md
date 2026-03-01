# Forecasting High Frequency Order Flow Imbalance

ref: 120
arxiv: https://arxiv.org/abs/2408.03594v1
Authors: Aditya Nittur Anantha, Shashi Jain
Published: 2024-08-07T07:16:06Z

## Abstract
Market information events are generated intermittently and disseminated at high speeds in real-time. Market participants consume this high-frequency data to build limit order books, representing the current bids and offers for a given asset. The arrival processes, or the order flow of bid and offer events, are asymmetric and possibly dependent on each other. The quantum and direction of this asymmetry are often associated with the direction of the traded price movement. The Order Flow Imbalance (OFI) is an indicator commonly used to estimate this asymmetry. This paper uses Hawkes processes to estimate the OFI while accounting for the lagged dependence in the order flow between bids and offers. Secondly, we develop a method to forecast the near-term distribution of the OFI, which can then be used to compare models for forecasting OFI. Thirdly, we propose a method to compare the forecasts of OFI for an arbitrarily large number of models. We apply the approach developed to tick data from the National Stock Exchange and observe that the Hawkes process modeled with a Sum of Exponential's kernel gives the best forecast among all competing models.

## Hypothesis
- Signed order-flow imbalance predicts short-horizon price pressure, especially when queue imbalance aligns with OFI direction.

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
- Suggested alpha_id: `forecasting_high_frequency_order_flow_imbalance`
- Scaffold: `python -m research scaffold forecasting_high_frequency_order_flow_imbalance --paper 120`
- Bridge flow: `python -m research paper-to-prototype 120 --alpha-id forecasting_high_frequency_order_flow_imbalance`
