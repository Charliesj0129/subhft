# Multi-Level Order-Flow Imbalance in a Limit Order Book

ref: 124
arxiv: https://arxiv.org/abs/1907.06230v2
Authors: Ke Xu, Martin D. Gould, Sam D. Howison
Published: 2019-07-14T15:00:54Z

## Abstract
We study the multi-level order-flow imbalance (MLOFI), which is a vector quantity that measures the net flow of buy and sell orders at different price levels in a limit order book (LOB). Using a recent, high-quality data set for 6 liquid stocks on Nasdaq, we fit a simple, linear relationship between MLOFI and the contemporaneous change in mid-price. For all 6 stocks that we study, we find that the out-of-sample goodness-of-fit of the relationship improves with each additional price level that we include in the MLOFI vector. Our results underline how order-flow activity deep into the LOB can influence the price-formation process.

## Hypothesis
- Order-flow signals from deeper LOB levels add predictive value versus top-of-book-only signals for the next tick return.

## Candidate Formula
- `alpha_t = sum_k (w_k * ofi_k_t), where w_k = 1 / max(1, k)`

## Relevant Features (lob_shared_v1)
- `l1_bid_qty`
- `l1_ask_qty`
- `depth_imbalance_ppm`
- `depth_imbalance_ema8_ppm`
- `spread_scaled`
- `mid_price_x2`
- `microprice_x2`

## Implementation Notes
- Suggested alpha_id: `multi_level_order_flow_imbalance_limit_order_book`
- Scaffold: `python -m research scaffold multi_level_order_flow_imbalance_limit_order_book --paper 124`
- Bridge flow: `python -m research paper-to-prototype 124 --alpha-id multi_level_order_flow_imbalance_limit_order_book`
