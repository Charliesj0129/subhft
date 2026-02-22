# Equilibrium Liquidity and Risk Offsetting in Decentralised Markets

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Equilibrium Liquidity and Risk Offsetting in Decentralised Markets
• **作者**： Fayçal Drissi, Xuchen Wu, Sebastian Jaimungal (Oxford & Toronto)
• **年份**： 2025 (December 2025; ArXiv Dec 24, 2025)
• **期刊/會議**： ArXiv:2512.19838 [q-fin.TR]
• **引用格式**： Drissi, F., Wu, X., & Jaimungal, S. (2025). Equilibrium Liquidity and Risk Offsetting in Decentralised Markets. arXiv preprint arXiv:2512.19838.
• **關鍵詞**： #DEX_Liquidity #CEX_DEX_Arbitrage #Risk_Offsetting #Stochastic_Control #LVR #Market_Microstructure
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Uniswap_v3_Liquidity]], [[Optimal_Execution]], [[LVR]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 現有的 DEX 研究（如 LVR 文獻）通常假設 LP 可以在 CEX 進行 "Perfect Replication"（無摩擦完美對沖）。
- 現實中，CEX 對沖有成本（Trading Fees, Spread, Market Impact）。
- LP 是風險厭惡的（Risk Averse），需要在「DEX 賺取的手續費」與「CEX 對沖成本 + Inventory Risk」之間權衡。

• **研究目的**：

- 建立一個經濟模型，內生化（Endogenize）LP 的流動性供給決策和 CEX 對沖策略。
- 探討在 CEX 存在摩擦的情況下，LP 應該如何調整 DEX 的 Liquidity Depth ($\kappa$)。

• **理論框架**：

- **Three-Stage Game**:
  1. LP 決定 DEX 的流動性深度 $\kappa$。
  2. LP 在 CEX 動態調整對沖頭寸 $\nu_t$（解決 stochastic control 問題）。
  3. Noise Traders 和 Arbitrageurs 在 DEX 交易。
- **LVR (Loss-Versus-Rebalancing)**: 明確建模為 $\frac{1}{2} \sigma^2 \int F^2 \partial_{11} \phi dt$。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **Optimal Hedging Strategy**:

- LP 的目標是最大化終端財富並最小化路徑上的 Inventory Risk 和 Trading Cost。
- 問題被轉化為 **FBSDE (Forward-Backward Stochastic Differential Equations)**。
- 證明了該問題可簡化為 **Differential Riccati Equation (DRE)**，存在唯一解。
- **最優策略成分**：
  1. **Tracking Component**: 部分複製 DEX 的頭寸變化（但因 CEX 成本而不完全複製）。
  2. **Speculative Component**: 利用私人信號（Private Signal $A_t$）進行投機。

• **主要結論**：

- 當 LP 風險厭惡係數增加或 CEX 對沖成本增加時，LP **不會** 增加 CEX 對沖力度，而是選擇 **減少 DEX 的流動性供給**。
- 這是因為減少 DEX 流動性可以直接降低 Adverse Selection（因為套利者利潤減少），從而減少對沖需求。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Imperfect Hedging**: 最優策略不是 Delta Neutral。LP 應該容忍一定的 Inventory Exposure 以節省 CEX 交易成本。
2. **Liquidity Withdrawal**: 當市場波動率（Fundamental Volatility）上升時，LP 會顯著撤回 DEX 流動性（降低 $\kappa$），因為 LVR 成本與 $\sigma^2$ 成正比。
3. **Signal Impact**: 如果 LP 有 Alpha（能預測價格），他們會更積極地在 CEX 交易，這反而可能支持他們在 DEX 提供更多流動性（因為他們能更好地管理風險）。

• **圖表摘要**：

- 論文推導了最優流動性深度 $\kappa^*$ 與波動率 $\sigma$ 的負相關關係。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 打破了 "Perfect Detla Hedging" 的迷思。在高手續費環境下（如 Crypto），頻繁對沖是自殺行為。
- 提供了計算 **Optimal Hedging Ratio** 的數學工具（Riccati Equation）。

• **對 HFT 的啟示**：

- **Market Making Strategy**: 我們在鏈上做市時，不能盲目對沖。應該計算一個 "No-Hedge Region"（類似 Paper 2 的 Optimal Band）。
- **Liquidity Estimation**: 如果我們觀察到鏈上流動性突然變薄，這可能意味著 Smart LPs 預測到波動率即將上升（Private Signal）。這是一個 **Signal**。

---

### 📝 寫作語料庫 (Citable Material)

• **策略描述**: "Rational, risk-averse LPs... manage risk primarily by reducing the reserves supplied to the DEX."
• **LVR 定義**: "The term $Y_t dF_t$ ... known as Loss-Versus-Rebalancing (LVR) ... is commonly interpreted as a measure of adverse selection costs."

---

### 🚀 行動清單 (Action Items)

- [ ] **實現 Riccati Solver**: 雖然複雜，但我們可以嘗試實現一個簡化版的 Riccati Solver，輸入當前的 Fee 差和 Volatility，輸出推薦的 Hedge Ratio（例如 0.6 而不是 1.0）。
- [ ] **監控 DEX Depth**: 將 Uniswap V3 ETH/USDT 的 Liquidity Depth 作為一個 Feature。流動性撤退通常領先於大波動。
