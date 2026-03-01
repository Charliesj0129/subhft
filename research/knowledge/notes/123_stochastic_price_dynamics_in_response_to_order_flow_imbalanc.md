# Stochastic Price Dynamics in Response to Order Flow Imbalance: Evidence from CSI 300 Index Futures

ref: 123
arxiv: https://arxiv.org/abs/2505.17388v1
Authors: Chen Hu, Kouxiao Zhang
Published: 2025-05-23T01:53:28Z

## Abstract
We conduct modeling of the price dynamics following order flow imbalance in market microstructure and apply the model to the analysis of Chinese CSI 300 Index Futures. There are three findings. The first is that the order flow imbalance is analogous to a shock to the market. Unlike the common practice of using Hawkes processes, we model the impact of order flow imbalance as an Ornstein-Uhlenbeck process with memory and mean-reverting characteristics driven by a jump-type Lévy process. Motivated by the empirically stable correlation between order flow imbalance and contemporaneous price changes, we propose a modified asset price model where the drift term of canonical geometric Brownian motion is replaced by an Ornstein-Uhlenbeck process. We establish stochastic differential equations and derive the logarithmic return process along with its mean and variance processes under initial boundary conditions, and evolution of cost-effectiveness ratio with order flow imbalance as the trading trigger point, termed as the quasi-Sharpe ratio or response ratio. Secondly, our results demonstrate horizon-dependent heterogeneity in how conventional metrics interact with order flow imbalance. This underscores the critical role of forecast horizon selection for strategies. Thirdly, we identify regime-dependent dynamics in the memory and forecasting power of order flow imbalance. This taxonomy provides both a screening protocol for existing indicators and an ex-ante evaluation paradigm for novel metrics.

## Hypothesis
- OFI 的衝擊不是瞬時的，而是遵循 **Ornstein-Uhlenbeck (OU) 均值回歸過程**（有記憶性）。
- 漂移項由跳躍型 Lévy 過程驅動（大單的非線性衝擊）。
- **Quasi-Sharpe ratio（response ratio）**: 以 OFI 為觸發點的交易成本效益比 — 直接對應平台的 Gate D Sharpe 門檻。
- 關鍵發現: OFI 的記憶性和預測力依市場**制度（regime）**而異 → 需要 regime-aware backtest（`research/backtest/regime_splitter.py`）。
- **核心 inefficiency**: CSI 300 期貨（Asian futures，與 TWSE 期貨高度可比）中，OFI OU-drift 預測未來 10-100ms 的對數報酬率。

## Relevant Features
- `ofi_l1` (idx 8): spot OFI（OU 過程的觀測）
- `ofi_l1_ema8` (idx 9): OU 短記憶代理（τ≈8 ticks）
- `ofi_l1_ema32` (idx 10): OU 長記憶代理（τ≈32 ticks）
- `ofi_sign_ema8` (idx 15): OFI 方向 EMA（regime 過濾器）
- `depth_imbalance_ema8_ppm` (idx 13): 配合 OFI 做 regime 篩選

## Implementation Notes
- **高度相關**: CSI 300 期貨 ≈ TWSE TX 期貨（相同市場結構、order-driven、亞洲時區）。結論可直接遷移。
- **Latency 警示**: 論文的 OFI 預測窗口 10-100ms 已超過 Shioaji sim API RTT（~36ms P95），實際 alpha half-life 需以 P95 latency 驗證（CLAUDE.md latency realism requirement）。
- **Regime dependency**: 用 `regime_splitter.py` 分割牛/熊/盤整市場，驗證 OFI OU-drift 各 regime 的 Sharpe。
- 新 alpha 想法: `ofi_ou_mean_reversion` — `ofi_l1_ema8 - ofi_l1_ema32`（OU 均值偏差） × `sign(depth_imbalance_ema8_ppm)`。O(1)，直接用現有 features。
- scaffold: `python -m research scaffold ofi_ou_mr --paper 123`
