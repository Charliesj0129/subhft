# Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book

ref: 125
arxiv: https://arxiv.org/abs/1512.03492v1
Authors: Martin D. Gould, Julius Bonart
Published: 2015-12-11T00:26:23Z

## Abstract
We investigate whether the bid/ask queue imbalance in a limit order book (LOB) provides significant predictive power for the direction of the next mid-price movement. We consider this question both in the context of a simple binary classifier, which seeks to predict the direction of the next mid-price movement, and a probabilistic classifier, which seeks to predict the probability that the next mid-price movement will be upwards. To implement these classifiers, we fit logistic regressions between the queue imbalance and the direction of the subsequent mid-price movement for each of 10 liquid stocks on Nasdaq. In each case, we find a strongly statistically significant relationship between these variables. Compared to a simple null model, which assumes that the direction of mid-price changes is uncorrelated with the queue imbalance, we find that our logistic regression fits provide a considerable improvement in binary and probabilistic classification for large-tick stocks, and provide a moderate improvement in binary and probabilistic classification for small-tick stocks. We also perform local logistic regression fits on the same data, and find that this semi-parametric approach slightly outperform our logistic regression fits, at the expense of being more computationally intensive to implement.

## Hypothesis
- **Inefficiency**: When the best-bid queue is much larger than the best-ask queue, liquidity providers on the bid side will be reluctant to cancel (losing queue priority), creating directional pressure. The static imbalance encodes short-horizon supply/demand skew **before** any trade or cancel event occurs.
- **Mechanism**: Large bid queue → market orders will exhaust ask first → mid-price moves up; large ask queue → reverse.

## Relevant Features
- `bid_qty` / `bids[0,1]` — best-bid queue size (L1 bid volume)
- `ask_qty` / `asks[0,1]` — best-ask queue size (L1 ask volume)
- Signal: `QI = (V_bid − V_ask) / (V_bid + V_ask)` → EMA-smoothed for noise reduction
- Lives in feature registry index 15 (`depth_imbalance_ema8_ppm`) but uses raw queue sizes rather than PPM-scaled LOBStatsEvent field

## Implementation Notes
- Scaffold: `python -m research scaffold queue_imbalance --tier TIER_2 --paper-refs 125`
- Signal range: [−1, +1]; EMA window ≈ 8 ticks for TWSE instruments
- Allocator Law: `__slots__` on class; no heap alloc in update()
- Precision Law: all internal math in float; signal output is `float` (not price)
- Latency profile: `shioaji_sim_p95_v2026-03-01`
- Gate D requires matching `feature_set_version="lob_shared_v1"`
