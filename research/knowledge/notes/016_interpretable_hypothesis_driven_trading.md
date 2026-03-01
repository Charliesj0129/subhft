# Interpretable Hypothesis-Driven Trading: A Rigorous Walk-Forward Validation Framework
ref: 016
arxiv: https://arxiv.org/abs/2512.12924
Authors: Gagan Deep, Akash Deep, William Lamptey (Texas Tech University)
Published: 2025 (December 2025; ArXiv Dec 16, 2025)

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Interpretable Hypothesis-Driven Trading: A Rigorous Walk-Forward Validation Framework for Market Microstructure Signals
• **作者**： Gagan Deep, Akash Deep, William Lamptey (Texas Tech University)
• **年份**： 2025 (December 2025; ArXiv Dec 16, 2025)
• **期刊/會議**： ArXiv:2512.12924 [q-fin.TR]
• **引用格式**： Deep, G., Deep, A., & Lamptey, W. (2025). Interpretable Hypothesis-Driven Trading: A Rigorous Walk-Forward Validation Framework. arXiv preprint arXiv:2512.12924.
• **關鍵詞**： #Walk_Forward_Validation #Algorithmic_Trading #Hypothesis_Driven #Market_Microstructure #Regime_Switching #Overfitting_Prevention
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Backtesting_Methodology]], [[Alpha_Factor_Design]], [[Regime_Detection]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 金融文獻中充斥著 "不可復現" 的高收益策略（Replication Crisis），主要原因是 **Overfitting (過擬合)** 和 **Lookahead Bias (前視偏差)**。
- 黑箱機模型（Neural Networks）缺乏可解釋性，難以通過監管審計。
- 大多數研究忽視了策略表現的 **Regime Dependence**（市場狀態依賴性）。

• **研究目的**：

- 提出一套嚴格的 **Walk-Forward Validation Framework**，強調信息隔離（Information Set Discipline）。
- 將 "Hypothesis-Driven"（基於假設）的信號生成與 RL 結合，確保可解釋性。
- 通過 10 年（2015-2024）的實證，展示 "誠實" 的回測結果應該是什麼樣的。

• **理論框架**：

- **Hypothesis**: $h = (s, a, \theta, \ell, c, x, r^*, \delta^*)$，其中 $\ell$ 是自然語言解釋。
- **Environment**: Rolling Window Walk-Forward (Train 252 days, Test 63 days, Step 63 days).
- **Agent**: Hypothesis Selection via $\epsilon$-Greedy Bandit.

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **Feature Engineering**:

- Focus on **High-Frequency Microstructure Signals from Daily Data** (e.g., Volume Imbalance, Volume Ratio, Price Efficiency).
- Key finding: 試圖從日線數據中提取微觀結構信號在低波動率時期極其困難。

• **Hypothesis Types**:

1. **Institutional Accumulation**: Buy Imbalance + Stable Price.
2. **Flow Momentum**: Price Momentum + Confirming Flow.
3. **Mean Reversion**: Oversold in Stable Regime.
4. **Breakout**: High Volume + New High.
5. **Range-Bound Value**: Range trading.

• **Validation Protocol**:

- 34 Independent Out-Of-Sample Folds.
- No parameter tuning on Test set.
- Realistic Cost Model: 5 bps slippage + Commission.

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Modest Returns**: 年化回報僅 0.55%，Sharpe 0.33。這與學術界常見的 "Sharpe 2.0+" 形成鮮明對比，反映了 **Honest Validation** 的結果。
2. **Regime Dependence**:
   - **High Volatility (2020-2024)**: 策略表現良好（Quarterly +0.60%），因為高波動率帶來了更多信息流（Information Flow），使得日線微觀信號有效。
   - **Low Volatility (2015-2019)**: 策略失效（Quarterly -0.16%），因為噪聲交易主導，微觀信號被淹沒。
3. **Risk Management**: 儘管回報低，但最大回撤僅 -2.76%（vs SPY -23.8%），表現出極強的抗風險能力（Market Neutral）。

• **圖表摘要**：

- **Table 3**: 清晰展示了 Low Vol vs High Vol 時期的表現差異。
- **Fig 3**: 累積收益曲線在 2020 年後顯著上升，而在 2019 年前持平。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 建立了一個 "回測標準"：如果你沒有做 Walk-Forward，你的結果就是不可信的。
- 揭示了 **Daily Data Microstructure Signals 的侷限性**：它們只在活躍市場中有效。這暗示了 HFT/Intraday 數據的必要性。
- 提倡 "Interpretable Hypothesis"：使用自然語言描述策略邏輯，這對於 LLM 輔助量化研究非常有啟發。

• **對 HFT 的啟示**：

- **Regime Switching is Mandatory**: 我們的策略必須包含一個 `VolatilityRegime` 開關。在低波環境下，要麼停止交易，要麼切換到專門的低波策略（如 Grid/Market Making），而不是試圖捕捉趨勢。
- **Data Granularity**: 不要指望用 Daily/Hourly 數據捕捉 Institutional Accumulation。必須用 Tick/Trade 數據計算 VPIN 或 OFI。
- **Expectation Management**: 真正的 Alpha 是稀缺且微薄的。如果回測跑出 Sharpe 3.0，首先懷疑代碼寫錯了。

---

### 📝 寫作語料庫 (Citable Material)

• **結論**: "Daily OHLCV-based microstructure signals require elevated information arrival and trading activity to function effectively."
• **警告**: "Institutional investors report that over 90% of academic strategies fail when implemented with real capital."

---

### 🚀 行動清單 (Action Items)

- [ ] **Review Backtest Pipeline**: 檢查我們的 `hft_backtest` 框架，確保它是嚴格的 Walk-Forward (Rolling Window)，而不是簡單的 Split。
- [ ] **Implement Volatility Filter**: 在所有動量/趨勢策略中加入 `volatility_threshold`，在低波時期自動休眠。
