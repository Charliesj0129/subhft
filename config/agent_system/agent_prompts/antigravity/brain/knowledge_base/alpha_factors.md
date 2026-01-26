# Significant Alpha Factors

> Generated: 2026-01-25 | Data: 30-day Heston-Hawkes LOB
> Analysis: IC-based (threshold |t| > 2.0) + hftbacktest Sharpe

## IC Analysis Results
|--------|----------|-----|--------|--------|----------|
| **OFI** | 2408.03594v1 | -0.0926 | **-8.33** | -81.28 | 35.3% |
| **MidMomentum** | 2110.00771v2 | -0.0729 | **-6.25** | -63.57 | 34.7% |
| **DepthImbalance** | 2410.08744v3 | -0.0291 | **-2.90** | -23.48 | 35.8% |
| **Spread** | 2510.08085v1 | +0.0254 | **+2.53** | -18.29 | 35.5% |
| **WOBI** | 2312.08927v5 | -0.0225 | **-2.24** | -21.34 | 36.2% |

## Factor Descriptions

### OFI (Order Flow Imbalance)
- **Paper**: [Forecasting High Frequency Order Flow Imbalance](../../../research/arxiv_papers/2408.03594v1_Forecasting_High_Frequency_Ord.pdf)
- **Formula**: `bid_flow - ask_flow` based on price/size changes (Cont et al. 2014)
- **Usage**: Short-term mean reversion signal (negative IC suggests counter-trend)

### MidMomentum
- **Paper**: [Non-average price impact in order-driven markets](../../../research/arxiv_papers/2110.00771v2_Non-average_price_impact_in_or.pdf)
- **Formula**: 5-period mid-price return
- **Usage**: Momentum factor (negative IC suggests reversal dominates)

### DepthImbalance
- **Paper**: [No Tick-Size Too Small](../../../research/arxiv_papers/2410.08744v3_No_Tick-Size_Too_Small:_A_Gene.pdf)
- **Formula**: `(total_bid_vol - total_ask_vol) / total`
- **Usage**: Order book pressure indicator

### Spread
- **Paper**: [A Deterministic Limit Order Book Simulator](../../../research/arxiv_papers/2510.08085v1_A_Deterministic_Limit_Order_Bo.pdf)
- **Formula**: `(ask - bid) / mid`
- **Usage**: Liquidity/volatility proxy (positive IC)

### WOBI (Weighted Order Book Imbalance)
- **Paper**: [Limit Order Book Dynamics and Order Size Modelling](../../../research/arxiv_papers/2312.08927v5_Limit_Order_Book_Dynamics_and_.pdf)
- **Formula**: Geometric-weighted OBI across 5 levels
- **Usage**: Multi-level depth signal

## Summary
- **Total factors tested**: 8
- **Significant (|t| > 2.0)**: 5
- **Best predictor**: OFI (t=-8.33)
---

## hftbacktest Simulation Results

| Factor | Sharpe | PnL | Trades | Verdict |
|--------|--------|-----|--------|---------|
| **WOBI** | **+16.51** | +416 | 30 | ✅ Best |
| **DepthImbalance** | **+14.71** | +371 | 30 | ✅ Strong |
| OBI | -15.90 | -367 | 150 | ❌ |
| Spread | -22.95 | -581 | 10 | ❌ |
| RealizedVol | -22.93 | -58 | 1 | ❌ |
| TradeImbalance | -48.80 | -928 | 989 | ❌ |
| MidMomentum | -53.48 | -905 | 989 | ❌ |
| OFI | -132.27 | -2027 | 989 | ❌ Worst |

### Key Insight
- **WOBI** and **DepthImbalance** have positive Sharpe in simulation
- OFI shows strong IC but negative Sharpe → **execution costs dominate**
- Lower trade frequency factors perform better (30 vs 989 trades)

---

## Updated Results (50K Events Dataset)

> 生成日期: 2026-01-25 | 資料: 50,000 events/day | Kurtosis=4.25

| Factor | Sharpe | PnL | Trades | Status |
|--------|--------|-----|--------|--------|
| **OBI** | **+3.29** | +149 | 10 | ✅ |
| **WOBI** | **+3.29** | +149 | 10 | ✅ |
| **Spread** | **+3.28** | +149 | 10 | ✅ |
| **RealizedVol** | **+3.20** | +15 | 1 | ✅ |
| DepthImbalance | +0.88 | +40 | 146 | ✅ |
| TradeImbalance | -76.33 | -2697 | 2728 | ❌ |
| OFI | -148.65 | -4352 | 3706 | ❌ |
| MidMomentum | -442.85 | -13842 | 5001 | ❌ |

### 結論
1. **低頻因子勝出**: OBI, WOBI, Spread (10 trades) > DepthImbalance (146) > 高頻因子
2. **OFI 悖論**: IC 最高但 Sharpe 最差 — 執行成本主導
3. **新增贏家**: RealizedVol 在大數據集上表現變好

---

## Full 14-Factor Results (100K Events)

> 生成日期: 2026-01-25 | 資料: 100,000 events

| Rank | Factor | Paper ID | Sharpe | PnL | Trades |
|------|--------|----------|--------|-----|--------|
| 1 | **QueuePressure** | 2511.18117v1 | **+4.20** | +196 | 10 |
| 2 | **PriceReversal** | 2110.00771v2 | **+2.46** | +227 | 10 |
| 3 | **OBI** | 2505.17388v1 | **+1.86** | +172 | 30 |
| 4 | **MicroPrice** | 2312.08927v5 | **+1.60** | +94 | 34 |
| 5 | **DepthSlope** | 2410.08744v3 | **+1.55** | +143 | 68 |
| 6 | WOBI | 2312.08927v5 | -2.58 | -235 | 20 |
| 7 | DepthImbalance | 2410.08744v3 | -2.67 | -247 | 10 |
| 8 | Spread | 2510.08085v1 | -2.68 | -247 | 10 |
| 9 | VolumeRatio | 2510.06879v1 | -2.68 | -247 | 10 |
| 10 | RealizedVol | 2503.14814v1 | -2.71 | -25 | 1 |
| 11 | TradeImbalance | 2506.07711v5 | -69.68 | -5071 | 5076 |
| 12 | SqrtImpact | 2506.07711v5 | -80.12 | -6116 | 6168 |
| 13 | OFI | 2408.03594v1 | -188.50 | -9646 | 7946 |
| 14 | MidMomentum | 2110.00771v2 | -483.17 | -29271 | 10001 |

### 新增因子說明
- **QueuePressure**: L1 委託量差 (bid - ask)
- **PriceReversal**: 價格偏離移動平均 (均值回歸)
- **MicroPrice**: 成交量加權微價格偏差
- **DepthSlope**: 深度衰減斜率差異

### 結論
> **低頻策略勝出**: Top 5 因子交易次數 10-68，高頻因子全虧損

---

## Recent q-fin.TR Papers (arXiv 2026 Jan)

> 搜尋日期: 2026-01-25 | 來源: arXiv q-fin.TR

| arXiv ID | 標題 | 關鍵概念 | 潛在因子 |
|----------|------|----------|----------|
| 2601.13421 | Market Making & Transient Impact (FX) | 暫態衝擊, 風險管理 | **TransientImpact** |
| 2601.11958 | Agentic AI Nowcasting (Sharpe 2.43!) | LLM 選股, 實時搜尋 | **SentimentScore** |
| 2601.11201 | Timescale Separation | 快慢分離, 均值回歸 | **SlowFastDecomp** |
| 2601.10591 | ProbFM Uncertainty Decomposition | 認知/隨機不確定性 | **UncertaintyRatio** |
| 2601.10143 | Adaptive Dataflow System | 概念漂移, 數據增強 | **DriftIndicator** |

### Web 搜尋發現的論文

| 主題 | 描述 | 潛在因子 |
|------|------|----------|
| Neural Hawkes LOB | 神經 Hawkes 強度建模 | **IntensityFeature** |
| Optimal Liquidity Provision | 完整/不完整信息下最優報價 | **InformationValue** |
| ARL Market Making + Hawkes | 可變波動率 + Hawkes 執行 | **VolatilityRegime** |
| Deep OFI Extraction | 多時間尺度 OFI alpha | **MultiHorizonOFI** |
| DiffVolume | LOB 成交量擴散模型 | **VolumeUncertainty** |
