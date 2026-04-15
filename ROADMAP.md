# ROADMAP — TAIFEX 深度量化平台 2 年成長路線圖

> **願景**：個人散戶 × 單機部署，專注 TAIFEX 一個市場做到極致 — 100+ 策略、極低延遲、全自動化 24/7 無人值守運維。
>
> **基線**：2026-04 起步，核心代碼 ~95K 行（Python 88K + Rust 7.5K），測試 175K 行，研究 176K 行，總計 ~67.6 萬行。
>
> **目標**：2028-04 達到 ~250 萬行級別完整系統，涵蓋 Alpha 工廠、智慧執行、自治運維三大支柱。
>
> 版本：v2.0 | 更新日期：2026-04-15

---

## 核心理念

**建造生產 alpha 的機器，而非手工生產 alpha。**

250 萬行代碼的構成不是 250 萬行策略，而是：

- **Alpha 工廠基礎設施** — 自動研究、回測、評估、上線流水線
- **執行演算法庫** — 拆單、智慧路由、成本模型、佇列預測
- **自動化運維** — 自愈、監控、異常檢測、災難恢復
- **資料工程** — 清洗、特徵儲存、回放引擎、替代資料

---

## 三條平行軌道

```
軌道 A: Alpha 工廠     → 從「手動研究」到「自動化 alpha 流水線」
軌道 B: 執行引擎       → 從「下單」到「智慧執行最佳化」
軌道 C: 自治運維       → 從「人工監控」到「全自動 24/7 自愈」
```

---

## Phase 1：Q2-Q3 2026（4月 → 10月）— 基底層

> 目標：建立 alpha 量產的基礎設施，修復執行品質的度量盲區，系統化自愈能力。

### 軌道 A — Alpha 工廠 v1

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| 統一因子庫 | 從 FeatureEngine 27 個特徵擴展到 200+，標準化 API（compute → signal → score） | 25K |
| 自動回測框架 | 標準化回測流程（資料載入 → 信號生成 → 模擬撮合 → 績效評估），取代手動 notebook | 30K |
| Alpha 搜尋引擎 v1 | 參數空間掃描 + 組合搜尋（Optuna），自動跑 Gate A-C，每日產出候選 alpha 清單 | 20K |
| 因子正交化 | 自動檢測因子相關性、去除冗餘、PCA / 殘差化 | 8K |

### 軌道 B — 執行品質基礎

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| TCA v2 | 完整交易成本分析：Implementation Shortfall 分解、maker/taker 分類、逐筆歸因 | 12K |
| 市場衝擊模型 | TAIFEX 專屬衝擊模型：基於歷史 L5 LOB 估算下單對價格的影響 | 8K |
| Fill Probability Model | 掛單成交概率模型：queue position、spread regime、volume profile | 10K |

### 軌道 C — 自治基礎

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| 自愈框架 v1 | 故障分類 → 自動診斷 → 自動修復 → 上報，系統化所有已知故障模式 | 15K |
| 運維狀態機 | SessionGovernor 擴展：開盤前檢查 → 交易中監控 → 收盤後清算 → 夜盤切換，全自動 | 10K |
| 告警分級路由 | Telegram 分級（INFO / WARN / CRITICAL / FATAL）+ 靜音規則 + 升級鏈 | 5K |

> 整合舊 WS-A（Feed 穩定化）、WS-B（WAL/CK 穩定吞吐）、WS-C（資料治理）於軌道 C。

**Phase 1 增量：~143K LOC → 累計 ~240K 核心代碼**

### Phase 1 里程碑

- [ ] Alpha 搜尋引擎每日自動產出 ≥ 5 個候選因子
- [ ] TCA 報告覆蓋 100% 已上線策略的每筆交易
- [ ] 系統連續 7 天無需人工介入
- [ ] Feed burn-in 連續 60 交易日無 crash-signature critical 告警
- [ ] WAL Insert failed 比率日 < 0.5%，CH 短故障後 10 分鐘內 backlog 回基線
- [ ] 每日資料品質報告全自動產出

---

## Phase 2：Q4 2026 - Q1 2027（10月 → 4月）— 產能擴張

> 目標：alpha 從「搜尋」進化到「演化」，執行從「下單」進化到「拆單」，運維從「自愈」進化到「預測」。

### 軌道 A — Alpha 工廠 v2

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| Alpha 遺傳演算法 | 因子表達式的 GP（Genetic Programming）搜尋，自動發現非線性因子組合 | 25K |
| 跨時間框架引擎 | 統一 tick / 1s / 1m / 5m / 1h 多時間尺度信號聚合，策略可混用不同頻率因子 | 15K |
| Shadow Trading 框架 | 自動化 Gate E：候選 alpha 上影子盤，即時對比 live 績效 vs 回測預期 | 20K |
| Portfolio 構建器 | 多策略組合最佳化：risk parity、max Sharpe、drawdown constraint | 15K |
| 因子衰減監控 | 自動偵測因子 IC 衰減、regime shift、策略失效 → 自動降權 / 下架 | 10K |

### 軌道 B — 智慧執行

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| TWAP / VWAP 拆單 | 大單拆解演算法，適配 TAIFEX 流動性 profile | 15K |
| Adaptive 執行策略 | 根據即時 LOB 狀態動態切換 aggressive / passive / peg | 20K |
| Maker 佇列模型 | 精確預測掛單在佇列中的位置，最佳化掛單價位和時機 | 12K |
| 執行模擬器 | 用歷史 L5 LOB 資料模擬執行品質，驗證執行演算法的改進效果 | 15K |

### 軌道 C — 深度自治

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| 異常檢測引擎 | 統計異常檢測（EWMA、isolation forest）監控所有 metrics，取代固定閾值 | 20K |
| 容量規劃 | 自動監控 CPU / MEM / Disk 趨勢，預測何時需要擴容或清理 | 8K |
| 合約到期自動處理 | TAIFEX 月結自動偵測 → 換月 → 持倉遷移 → 歷史合約歸檔 | 10K |
| 災難恢復演練 | 自動化 WAL replay 驗證、checkpoint 恢復測試、每週自動跑 | 8K |

> 整合舊 WS-D（部署治理）、WS-E（HA/DR）、WS-F（硬體管理）於軌道 C。

**Phase 2 增量：~193K LOC → 累計 ~433K 核心代碼**

### Phase 2 里程碑

- [ ] GP 搜尋引擎每週自動發現 ≥ 1 個通過 Gate C 的新因子
- [ ] Shadow 框架同時跑 ≥ 20 個候選策略
- [ ] 執行演算法將 R47 的 fill rate 提升 ≥ 10%
- [ ] 合約到期全自動切換，零人工介入
- [ ] 系統連續 30 天無需人工介入
- [ ] 季度 chaos drill 全通過，RTO ≤ 20 分鐘，RPO ≤ 1 分鐘
- [ ] 部署可回滾率 100%，每季 0 次未授權漂移

---

## Phase 3：Q2-Q3 2027（4月 → 10月）— 規模化

> 目標：alpha 從「演化」進化到「學習」，執行覆蓋選擇權多腿，運維實現全自動 AI 輔助診斷。

### 軌道 A — Alpha 工廠 v3

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| ML Alpha 平台 | LightGBM / XGBoost / NN 因子，統一訓練 → 驗證 → 部署流水線 | 40K |
| 替代資料接入 | 台灣特有資料源：法人籌碼、融資融券、選擇權 OI、外資期貨部位 | 25K |
| Alpha 組合最佳化 | 100+ alpha 的自動權重調整、相關性管理、尾部風險控制 | 20K |
| 策略生命週期管理 | 從誕生到退役的完整追蹤：研究 → 回測 → shadow → canary → production → decay → retire | 15K |
| 回測引擎 v2（Rust） | 高效能回測核心遷移到 Rust，支援逐 tick 回測 100+ 策略 × 3 年資料 < 10 分鐘 | 30K |

### 軌道 B — 執行品質 v2

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| 選擇權執行引擎 | TXO 多腿下單、delta hedge、Greeks 即時計算、波動率曲面 | 30K |
| 跨商品執行 | TX / MTX / TE 聯動執行，利用合約間 spread 最佳化進出場 | 15K |
| 執行歸因系統 | 每筆交易的成本歸因：timing、spread、impact、fee → 持續改進循環 | 12K |
| Anti-Gaming | 偵測對手方的掠奪性行為（spoofing、layering），避免被狙擊 | 10K |

### 軌道 C — 全自治

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| AI 運維助手 | LLM 驅動的異常分析：log 摘要 → 根因推斷 → 建議修復 → 自動執行 | 25K |
| 資料品質引擎 v2 | 即時資料一致性檢查、gap 修補、outlier 修正、跨源交叉驗證 | 15K |
| 績效報告自動化 | 每日 / 每週 / 每月自動生成策略績效報告、風險報告、執行品質報告 | 12K |
| 自動調參 | 基於近期市場 regime 自動微調策略參數（Bayesian optimization + 安全約束） | 15K |

**Phase 3 增量：~264K LOC → 累計 ~697K 核心代碼**

### Phase 3 里程碑

- [ ] 100+ alpha 同時 live（含 shadow + canary + production）
- [ ] ML 因子佔 alpha 信號的 ≥ 30%
- [ ] 選擇權策略上線並產生正 PnL
- [ ] AI 運維助手自動處理 ≥ 80% 的非 HALT 級異常
- [ ] 系統連續 90 天無需人工介入

---

## Phase 4：Q4 2027 - Q1 2028（10月 → 4月）— 極致化

> 目標：深度學習驅動的 alpha 發現，全 Rust 熱路徑極致延遲，系統自我進化。

### 軌道 A — Alpha 工廠 v4

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| 深度學習因子 | Transformer / LSTM 序列模型、Graph NN（LOB 結構）、Attention 機制 | 50K |
| 強化學習做市 | RL-based market making：最佳化 quoting 策略（Avellaneda-Stoikov 擴展） | 35K |
| 合成資料生成 | GAN / Diffusion 模型生成仿真市場資料，擴充訓練集、壓力測試 | 25K |
| 因子知識圖譜 | 所有因子的來源、依賴關係、衰減歷史、互動效應，圖形化管理 | 15K |
| Alpha 論文解析器 | 自動讀取 q-fin 論文 → 提取策略邏輯 → 生成回測代碼 → 初步評估 | 20K |

### 軌道 B — 極致執行

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| 全 Rust 熱路徑 | normalizer → LOB → feature → strategy → risk 全鏈路 Rust 化 | 40K (Rust) |
| 共享記憶體 IPC | Python orchestrator ↔ Rust engine 零拷貝通訊 | 10K (Rust) |
| 硬體最佳化 | CPU affinity、NUMA-aware、huge pages、kernel bypass（AF_XDP） | 8K |
| 預測執行 | 在信號確認前預先準備訂單，信號到達後 < 1μs 發送 | 12K |

### 軌道 C — 自我進化

| 模組 | 說明 | 預估 LOC |
|------|------|----------|
| 混沌工程框架 | 自動注入故障（網路延遲、CK 宕機、broker 斷線），驗證自愈能力 | 20K |
| 回歸偵測 | 每次部署自動對比前後版本的延遲、PnL、fill rate 差異 | 12K |
| 自動部署流水線 | git push → test → build → canary deploy → monitor → rollback / promote | 15K |
| 系統演化追蹤 | 自動記錄每次變更的影響、維護架構 fitness function | 8K |

**Phase 4 增量：~270K LOC → 累計 ~967K 核心代碼**

### Phase 4 里程碑

- [ ] 全 Rust 熱路徑延遲 < 10μs（normalizer → order out）
- [ ] 深度學習因子佔 alpha 信號的 ≥ 50%
- [ ] 混沌工程每週自動執行，MTTR < 60 秒
- [ ] 系統連續 180 天無需人工介入

---

## LOC 成長曲線

```
                Core     Test    Research  Infra/Docs    Total
現在 (Q2'26)     95K     175K      176K       230K       676K
Phase 1         238K     310K      200K       260K     1,008K
Phase 2         431K     500K      230K       300K     1,461K
Phase 3         695K     750K      280K       350K     2,075K
Phase 4         965K   1,000K      330K       400K     2,695K
```

---

## 單機硬體演進

| 時間點 | 建議配置 | 原因 |
|--------|---------|------|
| 現在 | 舊電腦（夠用） | Phase 1 開發在本機，部署在舊電腦 |
| Phase 2 | **升級主機**：Ryzen 9 / 64GB / NVMe 2TB | 100+ 策略 + ML 訓練 + 大量回測 |
| Phase 3 | 加 **GPU**（RTX 4060+） | 深度學習因子訓練 |
| Phase 4 | 考慮 **10G NIC** + kernel bypass | 極致延遲最佳化 |

---

## 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| Alpha 衰減速度 > 生產速度 | 策略庫萎縮 | Phase 2 GP + Phase 3 ML 自動化產能 |
| TAIFEX 流動性不足以支撐 100+ 策略 | 策略互相擠壓 | Portfolio 構建器管理容量、跨商品分散 |
| 單機硬體瓶頸 | 回測慢、延遲高 | Phase 4 全 Rust + 硬體升級 |
| 個人精力瓶頸 | 開發速度跟不上計畫 | AI 輔助開發（Claude Code）、自動化測試 |
| 監管風險 | 策略被限制 | 保守風控、合規檢查模組 |

---

## 成功判定標準

> 需連續 12 個月滿足。

### 可用性

- `hft-engine`、`hft-monitor`、`wal-loader`、`clickhouse`、`redis`、`prometheus` 月可用率 ≥ 99.95%
- `execution_gateway_alive`、`execution_router_alive` 月可用率 ≥ 99.9%

### 資料完整性

- 每日資料完整度（符號 / 時段覆蓋）≥ 99.5%
- `wal_backlog_files`：p95 ≤ 20，p99 ≤ 100
- `wal_replay_errors_24h = 0` 天數佔比 ≥ 99%

### 自治營運

- 非計畫性人工介入 ≤ 每季 1 次
- 關鍵事故 MTTD ≤ 5 分鐘，MTTR ≤ 20 分鐘

### 變更安全

- 100% 生產部署具備 pre-deploy 備份、回滾 tag、回滾腳本
- remote 與 `origin/main` 不得存在失控漂移

---

## 開發節奏

- **每季度**：1 個大模組上線 + 2-3 個小模組
- **每月**：1 次架構 review + 1 次績效 review
- **每週**：CI 全跑 + alpha 搜尋結果 review
- **每日**：自動化 soak test + 告警 review（目標：零人工）

---

## 附錄：舊版 WS 工作流整合對照

| 舊版工作流 | 整合至 |
|-----------|--------|
| WS-A Feed / Session 韌性 | Phase 1 軌道 C — 自愈框架、運維狀態機 |
| WS-B WAL / ClickHouse 穩定吞吐 | Phase 1 軌道 C — 自愈框架 |
| WS-C 資料治理與品質稽核 | Phase 1 軌道 C → Phase 3 軌道 C 資料品質引擎 v2 |
| WS-D 部署漂移控制 | Phase 2 軌道 C — 災難恢復演練 → Phase 4 自動部署流水線 |
| WS-E HA / 災難恢復 | Phase 2 軌道 C — 災難恢復演練 |
| WS-F 硬體與生命週期 | Phase 2 軌道 C — 容量規劃 |
| WS-G 熱路徑 Rust 化 | Phase 4 軌道 B — 全 Rust 熱路徑 |
| WS-H 研究工廠擴大 | Phase 1-2 軌道 A — Alpha 工廠 v1/v2 |
