# Optimal Execution with Dynamic Order Flow Imbalance

ref: 126
arxiv: https://arxiv.org/abs/1409.2618v2
Authors: Kyle Bechler, Mike Ludkovski
Published: 2014-09-09T07:16:25Z

## Abstract
We examine optimal execution models that take into account both market microstructure impact and informational costs. Informational footprint is related to order flow and is represented by the trader's influence on the flow imbalance process, while microstructure influence is captured by instantaneous price impact. We propose a continuous-time stochastic control problem that balances between these two costs. Incorporating order flow imbalance leads to the consideration of the current market state and specifically whether one's orders lean with or against the prevailing order flow, key components often ignored by execution models in the literature. In particular, to react to changing order flow, we endogenize the trading horizon $T$. After developing the general indefinite-horizon formulation, we investigate several tractable approximations that sequentially optimize over price impact and over $T$. These approximations, especially a dynamic version based on receding horizon control, are shown to be very accurate and connect to the prevailing Almgren-Chriss framework. We also discuss features of empirical order flow and links between our model and "Optimal Execution Horizon" by Easley et al (Mathematical Finance, 2013).

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
- Suggested alpha_id: `optimal_execution_dynamic_order_flow_imbalance`
- Scaffold: `python -m research scaffold optimal_execution_dynamic_order_flow_imbalance --paper 126`
- Bridge flow: `python -m research paper-to-prototype 126 --alpha-id optimal_execution_dynamic_order_flow_imbalance`
