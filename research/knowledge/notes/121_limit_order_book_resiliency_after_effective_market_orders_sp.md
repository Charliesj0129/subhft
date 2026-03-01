# Limit-order book resiliency after effective market orders: Spread, depth and intensity

ref: 121
arxiv: https://arxiv.org/abs/1602.00731v2
Authors: Hai-Chuan Xu, Wei Chen, Xiong Xiong et al.
Published: 2016-02-01T22:26:28Z

## Abstract
In order-driven markets, limit-order book (LOB) resiliency is an important microscopic indicator of market quality when the order book is hit by a liquidity shock and plays an essential role in the design of optimal submission strategies of large orders. However, the evolutionary behavior of LOB resilience around liquidity shocks is not well understood empirically. Using order flow data sets of Chinese stocks, we quantify and compare the LOB dynamics characterized by the bid-ask spread, the LOB depth and the order intensity surrounding effective market orders with different aggressiveness. We find that traders are more likely to submit effective market orders when the spreads are relatively low, the same-side depth is high, and the opposite-side depth is low. Such phenomenon is especially significant when the initial spread is 1 tick. Although the resiliency patterns show obvious diversity after different types of market orders, the spread and depth can return to the sample average within 20 best limit updates. The price resiliency behavior is dominant after aggressive market orders, while the price continuation behavior is dominant after less-aggressive market orders. Moreover, the effective market orders produce asymmetrical stimulus to limit orders when the initial spreads equal to 1 tick. Under this case, effective buy market orders attract more buy limit orders and effective sell market orders attract more sell limit orders. The resiliency behavior of spread and depth is linked to limit order intensity.

## Hypothesis
- 流動性衝擊後，spread 和 depth 在 ~20 個 best-limit 更新內回歸均值。
- 侵略性市場單後，spread 暫時擴大但快速收斂 → **均值回歸訊號**。
- 非侵略性市場單後，價格延續（同向）。
- 買賣雙方 limit order 補充不對稱 → depth imbalance 的方向預測近期 fill probability。
- **核心 inefficiency**: spread 偏離 EMA 時，下一 tick 回歸概率高；depth imbalance 方向給出偏斜。

## Relevant Features
- `spread_scaled` (idx 3): 當前 bid-ask spread，scaled
- `depth_imbalance_ppm` (idx 2): L1 深度不平衡 ppm
- `spread_ema8_scaled` (idx 14): spread EMA-8（均值代理）
- `depth_imbalance_ema8_ppm` (idx 15): depth imbalance EMA-8
- `bid_depth_l1` (idx 3) / `ask_depth_l1` (idx 4): 買賣方 L1 絕對深度

## Implementation Notes
- **已關聯 alpha**: `spread_pressure` — formula `spread_diff × sign(depth_imb_ema8) / max(|spread_ema8|,1)` 直接實現本論文的 spread reversion + depth skew 機制。
- 本論文的中國市場（深交所/上交所）與 TWSE 高度相似（tick-based、order-driven），適用性高。
- 潛在新 alpha: LOB 彈性計時器 — 量化「spread 從衝擊恢復到 ±2σ 內所需 best-limit 更新數」作為 feature，用於自適應做市策略。
- Gate A data fields: `bid_px`, `ask_px`, `bid_qty`, `ask_qty`（已在平台 feature engine 實現）。
