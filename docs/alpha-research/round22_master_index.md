# Round 22: LOB-Driven Alpha — Master Survey Index

**Date**: 2026-03-28
**Scope**: 從 tick+bidask 資料出發，窮盡搜索 LOB 可驅動的所有預測方向
**總計**: 7 份調查文件, ~100 篇論文, 46 個搜索方向

---

## 調查文件索引

| # | 文件 | 論文數 | 核心問題 |
|---|------|--------|---------|
| 1 | [round22_stage1_literature_survey.md](round22_stage1_literature_survey.md) | 15 | 原始方向：LOB slope/convexity (已被 Challenger REJECT) |
| 2 | [round22_extended_lob_possibilities_survey.md](../outputs/team_artifacts/alpha-research/round22_extended_lob_possibilities_survey.md) | 23 | LOB 時間動態、flow 分解、metaorder、resilience |
| 3 | [round22_execution_optimization_survey.md](round22_execution_optimization_survey.md) | 12 | 執行品質優化 (limit/market 切換) |
| 4 | [round22_volatility_regime_survey.md](../outputs/team_artifacts/alpha-research/round22_volatility_regime_survey.md) | 10 | LOB → 波動率/regime 預測 |
| 5 | [round22_tick_patterns_survey.md](round22_tick_patterns_survey.md) | 26 | Tick 到達模式、Hawkes、duration model |
| 6 | [round22_bidask_dynamics_survey.md](round22_bidask_dynamics_survey.md) | 25 | BidAsk snapshot 序列動態、事件分類 |
| 7 | [round22_trade_classification_survey.md](round22_trade_classification_survey.md) | ~20 | Trade buy/sell 推斷方法 (解鎖基礎建設) |
| 8 | [round22_cross_frequency_survey.md](round22_cross_frequency_survey.md) | 27 | 跨頻率聚合：tick → 30s-5min 信號 |

---

## 專案資料優勢

```
TickEvent:   price(int x10000), volume, exch_ts(ns), local_ts(ns)
BidAskEvent: bids[5][price,vol], asks[5][price,vol], exch_ts(ns)
LOBStatsEvent: mid_price_x2, spread_scaled, imbalance, best_bid/ask, bid/ask_depth
FeatureEngine v2: 21 features per tick (OFI, spread EMA, imbalance EMA, depth momentum, etc.)
ClickHouse: 全歷史 L1-L5 存儲
Rust hot-path: PyO3 zero-copy, ring buffer bus
```

---

## 所有候選方向統整 (按優先級)

### Tier 0: 立即可做，高確信度

| 方向 | 機制 | 論文 | Horizon | 資料 | LOC | 預期價值 |
|------|------|------|---------|------|-----|---------|
| **瞬時波動率不變量** | `σ = spread × √(V_traded/depth) × P(spread/tick)` | Danyliv 2019 | 5-60 min | L1 | ~20 | CBS regime gating → Sharpe +10-20% |
| **執行優化器** (limit/market 切換) | fill_prob = f(Q_near, Q_opp, imbalance), R²=0.946 | Albers 2025 | per-trade | L1 | ~150 | -0.45 pts/trade = -11% RT cost |
| **HAR-style 多窗口聚合** | 3-window EMA (5s/30s/300s) on existing 21 features | Corsi 2009 | 30s-5min | existing | ~100 | 將 tick 信號延伸到可交易 horizon |

### Tier 1: 低成本探索，需要 Gate Zero 驗證

| 方向 | 機制 | 論文 | Horizon | 資料 | LOC | 關鍵問題 |
|------|------|------|---------|------|-----|---------|
| **Trade classification (EMO)** | at-bid/at-ask + tick rule fallback, 85-90% accuracy | Jurkatis 2020 | 基礎建設 | tick+bidask | ~100 | 解鎖 signed OFI, Hawkes, metaorder |
| **Hawkes branching ratio** | endogeneity measure, 從 tick timestamps 直接計算 | Hardiman 2013 | minutes | tick timestamps | ~50 | 不需要 trade classification |
| **Sym/Antisym OFI 分解** | delta_bid ± delta_ask decomposition | Elomari-Kessab 2024 | minutes | L1 depth | ~30 | VAR 參數穩定性待驗 |
| **Trade sign autocorrelation** | 滾動窗口 autocorrelation, 突降 = 大戶進場 | Primicerio 2018 | minutes | classified trades | ~30 | 需要先做 trade classification |
| **Tick-rate vol estimator** | tick_count(30s)/tick_count(300s) 加速比 | Lee 2019 | 30s-5min | tick timestamps | ~20 | 與 VRR 正交性待驗 |
| **Cancellation rate asymmetry** | bid/ask depth decrease 率不對稱 | Anantha 2025 | meso-scale | L1 snapshot diff | ~40 | 從 snapshot diff 推斷 |
| **Log-GOFI stationarization** | log(1 + |OFI|) × sign(OFI) — 簡單改良現有 OFI | Su 2021 | same as OFI | existing OFI | ~5 | 幾乎零成本測試 |

### Tier 2: 中成本原型，理論有支持

| 方向 | 機制 | 論文 | Horizon | 資料 | LOC | 關鍵風險 |
|------|------|------|---------|------|-----|---------|
| **Metaorder 偵測** | 從公開交易重建大單拆分 | Maitrier/Bouchaud 2025 | minutes | classified trades | Med | 需要 trade classification 先行 |
| **LO arrival/cancel rate asymmetry** | 限價單流動態 > 市價單流預測力 | Bechler 2017 | meso-scale | event inference | Med | 事件推斷精度未知 |
| **Intensity burst detection** | 異常 tick 密度 → vol/drift burst | Christensen 2024 | minutes | tick timestamps | ~50 | 閾值校準 |
| **Local Hurst exponent** | H₀ ≈ 3/4 偏離 → regime shift | Muhle-Karbe 2026 | minutes | signed trades | ~50 | 估計窗口長度 tradeoff |
| **Spread widening duration** | survival model: 寬 spread 持續 = vol regime | Panayi 2014 | minutes | L1 spread | ~30 | TMFD6 spread 變化可能太離散 |
| **LOB KE 近似** (depth change rate²) | `KE ≈ Σ(Δdepth[i]²)/dt` | Li 2023 | 1-30 min | L1-L5 | ~50 | L5 稀疏問題 |
| **Event-driven aggregation** | 在顯著價格變動之間聚合（而非固定窗口） | Elomari-Kessab 2024 | variable | tick+bidask | ~80 | 實作複雜度 |
| **Persistent depth change ratio** | 過濾 fleeting noise，只保留持久深度變化 | Filtration 2025 | meso-scale | L1 snapshot diff | ~40 | 定義 "persistent" 的閾值 |

### Tier 3: 高成本或理論性

| 方向 | 機制 | 論文 | 備註 |
|------|------|------|------|
| Path signatures | 非參數序列編碼 | Lyons/Kidger | 離線 feature discovery only |
| Wavelet decomposition | 尺度分離 | various | 理論清楚，實作複雜 |
| Full PCA mode decomposition | minute-scale coarse-graining | Elomari-Kessab 2024 | 需要大量工程 |
| Neural HMM regime | vol-adaptive granularity | Hu 2026 | 簡化版 (gating concept) 已在 Tier 1 |

### 已確認死亡

| 方向 | 原因 | Round |
|------|------|-------|
| LOB slope/convexity (原始 R22) | L2-L5 median 1 lot, 信號退化為離散雜訊 | R22 Challenger |
| Deep learning on LOB | 成本調整後準確率驟降，結構性障礙 | R16/TLOB |
| Cross-asset OFI (TSMC→TMFD6) | IC=0.061, p=0.066, 邊際 | R17 |
| VPIN regime overlay | DD -30.6%, BVC 分類誤差是 artifact | R12 |
| MLOFI gradient | Gate C FAIL, fees > returns | R11 |
| LOB KE/gravity center | IC 太弱, L3-L5 adds noise | R15 |

---

## 結構性發現

### 1. 資料層級的價值衰減
```
L1 ≈ 70% of LOB information
L2-L3 ≈ 20% additional (but noisy on thin books)
L4-L5 ≈ noise on TAIFEX
```
**結論**: L1 是主戰場，L2-L3 是條件性增量，L4-L5 放棄。

### 2. 信號壽命的物理限制
```
Snapshot features (imbalance, depth): half-life < 1s
Flow features (OFI, signed volume): half-life 1-15s
Aggregated flow (rolling OFI, HAR): half-life 30s-5min
Regime indicators (branching ratio, vol): half-life 5-60min
Structural patterns (metaorder, inventory): half-life hours
```
**結論**: 要預測更遠，必須從 "snapshot → flow → regime → structure" 逐層提升抽象層級。

### 3. Trade Classification 是最大的基礎建設缺口
```
Without classification:  OFI, depth change, spread, vol → 已測試，多數已耗盡
With classification:     signed OFI (+2-3x IC), Hawkes branching, metaorder detection,
                         toxic flow, adverse selection → 全部未探索
```
**結論**: 投資 ~100 LOC 做 EMO trade classification = 解鎖整個 signed flow 研究線。

### 4. 波動率預測 >> 方向預測
```
Direction prediction:  R² < 0.01 (all papers, all methods, after costs)
Volatility prediction: R² > 0.50 (closed-form formula, no training needed)
```
**結論**: 不要再找 directional alpha from LOB。用 LOB 預測 vol/regime → 調節現有策略參數。

### 5. Flow 分解 > 原始聚合
```
Raw OFI:        IC decays in seconds
Signed OFI:     2-3x IC improvement
Sym/Antisym:    minute-scale VAR prediction
Core/Reaction:  Hurst ≈ 3/4 (long memory)
Cluster-based:  30-min bucket signals
```
**結論**: 不是 "更好的聚合方式" 能拯救 OFI，而是 "不同的分解方式" 能提取不同的信息。

---

## 推薦執行路線圖

```
Week 1: 基礎建設
├── [T0.1] 瞬時波動率 instantaneous_vol_x1000 (20 LOC) → FeatureEngine slot [21]
├── [T0.2] EMO trade classifier (100 LOC) → Normalizer stage
└── [T0.3] HAR 3-window EMA aggregator (100 LOC) → FeatureEngine extension

Week 2: Gate Zero 驗證
├── [T1.1] Hawkes branching ratio diagnostic (tick timestamps)
├── [T1.2] Signed OFI IC vs unsigned OFI IC (需要 T0.2)
├── [T1.3] Sym/antisym OFI decomposition IC
├── [T1.4] Trade sign autocorrelation regime (需要 T0.2)
└── [T1.5] Tick-rate vol vs VRR 正交性

Week 3: 執行優化
├── [T0.4] CBS 執行優化器 prototype (150 LOC)
├── [T0.5] CBS regime-adaptive parameters (用 T0.1 的 vol)
└── [T2.1] Metaorder detection prototype (需要 T0.2)

Week 4: 回測驗證
├── Gate C: 最佳候選信號
├── Challenger review
└── Execution review
```

---

## 關鍵論文清單 (按 actionability 排序)

| Priority | Paper | Year | Key Contribution |
|----------|-------|------|-----------------|
| **P0** | Danyliv & Bland — Instantaneous Vol | 2019 | 閉合公式 vol estimator, MSE << GARCH 200x |
| **P0** | Albers et al. — Fill Prob vs Returns | 2025 | R²=0.946 fill prediction from 3 features |
| **P0** | Corsi — HAR-RV | 2009 | 3-window aggregation baseline |
| **P1** | Jurkatis — Trade Classification in Fast Markets | 2020 | 95% accuracy, open-source code |
| **P1** | Elomari-Kessab et al. — Microstructure Modes | 2024 | Sym/antisym decomposition, stable VAR |
| **P1** | Hardiman et al. — Hawkes Branching Ratio | 2013 | Endogeneity measure from timestamps |
| **P1** | Primicerio/Challet — Large Trader Detection | 2018 | Autocorrelation drop = large player entry |
| **P1** | Lee — Hawkes Volatility | 2019 | Closed-form vol from tick timestamps |
| **P2** | Maitrier/Bouchaud — Metaorder Detection | 2025 | Reconstruct institutional orders from public data |
| **P2** | Bechler/Ludkovski — Meso-Scale LOB | 2017 | LO flow > MO flow prediction power |
| **P2** | Lehalle/Mounjid — Latency vs Limit Orders | 2016 | 36ms → fire-and-forget policy |
| **P2** | Zhong et al. (KANFormer) — Fill Survival | 2025 | CAC40 futures, fill degrades to random at 30s+ |
| **P2** | Muhle-Karbe et al. — Unified Theory | 2026 | H₀≈3/4 pins all market quantities |
