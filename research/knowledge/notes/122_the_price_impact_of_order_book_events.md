# The Price Impact of Order Book Events

ref: 122
arxiv: https://arxiv.org/abs/1011.6402v3
Authors: Rama Cont, Arseniy Kukanov, Sasha Stoikov
Published: 2010-11-29T22:05:46Z

## Abstract
We study the price impact of order book events - limit orders, market orders and cancelations - using the NYSE TAQ data for 50 U.S. stocks. We show that, over short time intervals, price changes are mainly driven by the order flow imbalance, defined as the imbalance between supply and demand at the best bid and ask prices. Our study reveals a linear relation between order flow imbalance and price changes, with a slope inversely proportional to the market depth. These results are shown to be robust to seasonality effects, and stable across time scales and across stocks. We argue that this linear price impact model, together with a scaling argument, implies the empirically observed "square-root" relation between price changes and trading volume. However, the relation between price changes and trade volume is found to be noisy and less robust than the one based on order flow imbalance.

## Hypothesis
- **線性 OFI 價格衝擊**: `ΔP ≈ β × OFI / depth`，β 穩定跨時間尺度和股票。
- OFI 定義: `(ΔV_bid at best_bid) - (ΔV_ask at best_ask)` — 本平台的 `ofi_l1` feature（idx 8）。
- Slope β ∝ 1/depth → depth 小時 OFI 衝擊更大（本論文最重要的構型條件）。
- price change vs trade volume 雜訊高，但 OFI 更 robust → 平台以 OFI 為主特徵是正確方向。
- **核心 inefficiency**: OFI 超出 ±1σ 時，下一 best-quote 移動方向有統計顯著偏差。

## Relevant Features
- `ofi_l1` rolling (idx 8): L1 order flow imbalance — 本論文的核心量
- `ofi_l1_ema8` (idx 9): OFI 短期 EMA（信號平滑）
- `ofi_l1_ema32` (idx 10): OFI 中期 EMA（trend filter）
- `depth_imbalance_ppm` (idx 2): 作為 slope β 的反比代理
- `bid_depth_l1` (idx 3) / `ask_depth_l1` (idx 4): 計算 depth-adjusted OFI

## Implementation Notes
- **理論基礎**: `ofi_mc` alpha（ref 018）的奠基論文；本 ref 122 應補充到 `ofi_mc` 的 `paper_refs`。
- **關鍵實作注意**: β∝1/depth → 在 depth 極小的台股小型期貨上，OFI 衝擊可能非線性放大 → promotion Gate D 必須以 P95 latency 而非均值測試。
- 本論文的 NYSE TAQ 結果已在多個亞洲市場複製（含台灣，見 spread_pressure 研究）。
- 新 alpha 想法: `depth_adj_ofi` = `ofi_l1_ema8 / max(bid_depth_l1 + ask_depth_l1, 1)` — depth-normalized OFI。
- scaffold: `python -m research scaffold depth_adj_ofi --paper 122`
