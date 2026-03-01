# AutoQuant: An Auditable Expert-System Framework for Execution-Constrained Auto-Tuning
ref: 010
arxiv: https://arxiv.org/abs/2512.22476
Authors: Kaihong Deng
Published: 2025 (December 2025; ArXiv Dec 27, 2025)

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： AutoQuant: An Auditable Expert-System Framework for Execution-Constrained Auto-Tuning in Cryptocurrency Perpetual Futures
• **作者**： Kaihong Deng
• **年份**： 2025 (December 2025; ArXiv Dec 27, 2025)
• **期刊/會議**： ArXiv:2512.22476 [q-fin.TR]
• **引用格式**： Deng, K. (2025). AutoQuant: An Auditable Expert-System Framework for Execution-Constrained Auto-Tuning in Cryptocurrency Perpetual Futures. arXiv preprint arXiv:2512.22476.
• **關鍵詞**： #Backtesting_Framework #Expert_System #Execution_Constraints #Crypto_Perpetuals #Bayesian_Optimization #Auditability
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Backtest_Architecture]], [[Parameter_Tuning]], [[Risk_Management]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 加密貨幣永續合約（Perpetuals）市場存在複雜的市場微結構（Funding Rates, Slippage, Liquidity Crises）。
- 現有的回測框架（如 Backtrader, Zipline）往往忽略了這些 "Frictions"（摩擦成本），導致回測結果嚴重虛高（Performance Inflation）。
- 缺乏一個 **Auditable (可審計)** 的流程：現有的策略開發往往是 "Parameter Tuning" 的黑箱操作，容易導致 Overfitting 且無法在實盤中復現。

• **研究目的**：

- 提出 **AutoQuant**：一個針對 Crypto Perps 的 Expert-System 框架。
- **Execution-Centric**: 強制執行嚴格的 T+1 執行邏輯（Strict T+1 Execution）和 Funding Rate Alignment。
- **Auditable**: 生成確定性的 Artifacts（配置參數、審計日誌），確保 "Offline Backtest" 與 "Live Execution" 的會計恆等式（Accounting Invariants）一致。
- **Double-Screening**: 結合貝葉斯優化（Stage I）和多場景魯棒性篩選（Stage II）。

• **理論框架**：

- **Expert System Decomposition**: 將系統分為 Knowledge Base（規則庫：T+1, Cost Models）、Inference Engine（推理引擎：TPE 優化器）和 Explanation Interface（解釋接口：審計報告）。
- **Accounting Invariants**: 定義了一組數學恆等式（如 Total PnL = Raw PnL - Fees - Funding），用於驗證系統的一致性。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **STRICT4H Protocol**：

- **T+1 Execution**: 信號在 Bar $t$ 收盤時計算，執行嚴格在 Bar $t+1$ 開始時進行（使用 Open Price 或 VWAP）。杜絕 Lookahead Bias。
- **Funding Alignment**: Funding Rate 作為外部時間序列，嚴格對齊到 Bar 的時間戳，禁止使用未來 Funding Rate 計算過去收益。

• **二階段篩選 (Two-Stage Screening)**：

- **Stage I (Bayesian Search)**:
  - 使用 TPE (Tree-structured Parzen Estimator) 在 Training Window 上進行參數搜索。
  - 目標函數：Annualized Net Return (After Costs)。
  - 約束：Realistic Constraints (Leverage, Exposure)。
- **Stage II (Double Screening)**:
  - 不再進行優化，而是對 Stage I 的 Top Candidates 進行 **Stress Testing**。
  - **Held-Out Window**: 在未見過的時間段（Validation Set）上測試。
  - **Cost Scenario**: 在不同的成本假設下（如 Fee x 1.5, Funding x 2.0）測試策略的生存能力。

• **審計機制 (Auditability)**：

- 輸出 `configuration.json` 和 `audit_log.csv`。
- 強制檢查：Backtest Engine 的逐筆成交記錄必須能通過 "Replay" 與實盤日誌完全對賬。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Performance Inflation**: 忽略成本（Frictionless）的回測會將年化收益誇大數倍。AutoQuant 的 STRICT4H 設置揭示了許多 "High Sharpe" 策略在考慮真實 Funding Cost 後實際上是虧損的。
2. **Robustness**: 經過 Double Screening 選出的參數組合，在 Out-of-Sample 測試中的 Drawdown 顯著更小，且更穩定。
3. **Parameter Fragility**: 許多參數在 Training Set 上表現極好，但在 Cost Scenario Stress Test 中崩潰，證明了單一場景優化的脆弱性。

• **圖表摘要**：

- **Fig 1**: AutoQuant 流程圖，清晰展示了從 Raw Data 到 Stage I (Search) 再到 Stage II (Screening) 的漏斗結構。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 將 "Software Engineering" 和 "Audit" 的概念引入量化研究。這對於機構化 HFT 至關重要。
- **"Strict T+1"**: 這是一個簡單但經常被忽視的規則。很多回測引擎允許在 Bar 內部成交（Cheat-on-Close），AutoQuant 強制 T+1 雖然保守，但最安全。

• **對 HFT 的啟示**：

- **Backtest Engine Upgrade**: 我們應該檢查我們的回測引擎是否嚴格遵守 T+1 和 Funding Alignment。特別是 Funding Rate，很多時候數據源的 Funding 是 "Next Payment"，容易造成 Lookahead。
- **Calibration Pipeline**: 我們的參數優化（如 Paper 3 的軌跡優化參數）應該採用類似的 Two-Stage Process：先優化，再在不同 Cost Scenario 下進行 Stress Test。

---

### 📝 寫作語料庫 (Citable Material)

• **定義 (AutoQuant Philosophy)**: "AutoQuant is an execution-centric, alpha-agnostic framework that can be viewed as an auditable expert system for strategy configuration selection."
• **警語**: "Frictionless backtests can produce abundant high-Sharpe momentum signals, but funding and slippage materially compress these... opportunities."

---

### 🚀 行動清單 (Action Items)

- [ ] **審計 Backtester**: 檢查 `hft_backtest` 的源代碼，確認 `Strict T+1` 邏輯。如果非 T+1，必須增加一個 Optional Flag `strict_execution=True`。
- [ ] **實現 Double Screening**: 在我們的 Alpha 研究流程中，增加一個 "Cost Sensitivity Test" 步驟。對於任何 Alpha，必須在 1.5x Fee 和 2x Funding 的假設下仍然盈利才算通過。
