# Agent-Based Simulation of a Perpetual Futures Market

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Agent-Based Simulation of a Perpetual Futures Market
• **作者**： Ramshreyas Rao
• **年份**： 2025 (Based on metadata, though likely a thesis/preprint)
• **期刊/會議**： Likely Thesis / Working Paper
• **引用格式**： Rao, R. (2025). Agent-Based Simulation of a Perpetual Futures Market.
• **關鍵詞**： #Perpetual_Futures #Agent-Based_Model #Crypto_Derivatives #Funding_Rate #Microstructure
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Market_Microstructure]], [[Crypto_Perpetuals]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- Perpetual Futures (Perps) 是加密貨幣市場的主流衍生品，但在傳統金融文獻中研究極少。
- 現有 Agent-Based Model (ABM) 多針對股票市場（如 Santa Fe Stock Market, Chiarella et al.），缺乏針對 Perps 特有的 **"Funding Rate (資金費率)"** 機制的研究。
- 需要理解 Funding Rate 如何作為一個負反饋機制（Negative Feedback Loop）有效地將 Perp 價格釘住現貨價格（Pegging）。

• **研究目的**：

- 擴展 Chiarella et al. (2002) 的限價訂單簿（LOB）模型，使其適應 Perp 市場。
- 引入兩類新 Agent 行為：**Positional Trading (方向性交易)** 和 **Basis Trading (基差/費率套利)**。
- 探討不同市場參數（如 Order Book Depth, Trade Bias）如何影響 "Peg" 的穩定性。

• **理論框架**：

- **Agent-Based Computational Economics (ACE)**.
- **Limit Order Book (LOB) Simulation**: 雙邊拍賣機制。
- **Funding Rate Mechanism**: 作為價格回歸的驅動力。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **類比與模型 (The Simulation)**：

- **基礎資產 (Spot)**: 由幾何布朗運動 (GBM) 生成，作為外部信號。
- **Agents**:
  1.  **Fundamentalist**: 被移除，因為 Perps 沒有內在價值，而是釘住 Spot。
  2.  **Chartist (趨勢跟蹤)**: 觀察 Spot 價格（Positional）或 Funding/Premium 歷史（Basis）來預測。
  3.  **Noise Trader (噪聲交易)**: 隨機交易提供流動性。
- **交易者類型 (Trader Types)**：
  - **Positional Traders**: "Buy Low, Sell High" (基於 Spot 價格)。
  - **Basis Traders**: 賺取 Funding Rate (基於 Premium)。如果 Premium > 0 (Perp > Spot)，Short Perp 賺取費率。
- **機制**: 每個時間步，新的 Agent 進入市場，舊的 Agent 隨機退出。Order Book 保留最新的 $\tau$ 個訂單。

• **實驗設計**：

- 使用 Shewhart Control Charts (控制圖) 來監控 Premium (= Perp Price - Spot Price) 的穩定性。
- 參數掃描：$\tau$ (Order Lifetime), Bias (Long/Short 偏好), Cohort Size (流動性)。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Peg 的湧現 (Emergence of Peg)**：即使是簡單的 Agents，只要引入 Funding Rate 動機，Perp 價格就會自然地釘住 Spot 價格。
2. **Basis Bias**:
   - 觀察到現實中 Long Traders 傾向於 Positional (投機)，而 Short Traders 傾向於 Basis Trading (期現套利)。
   - 模擬顯示，當這種 Bias 存在時，Perp 會長期處於 **Premium (正溢價)** 狀態。這解釋了為什麼牛市中加密貨幣 Perps 費率通常為正。
3. **Order Book Depth**: 增加 $\tau$ (訂單存活時間) 會收窄 Spread，提高 Peg 的緊密度。

• **圖表摘要**：

- **Fig 6**: 展示了 Chartist vs Noise Trader 權重對 Peg 穩定性的影響。混合策略最穩定。
- **Fig 11**: 展示了 Bias 參數如何導致 Premium 的均值（Center）偏離 0。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 提供了一個開源的 R 代碼實現（附錄中）來模擬 Perp 市場。
- 將 "Funding Rate Arbitrage" 明確建模為 Agent 行為，填補了 ABM 在 Crypto 領域的空白。

• **局限性**：

- **Liquidation (清算)**: 模型似乎沒有包含保證金清算（Liquidation Cascade）機制，這是 Perp 市場最極端的特徵（插針）。
- **Spot Impact**: 假設 Spot 是外生的 (Exogenous)，忽略了 Perp 價格對 Spot 的反身性影響（Reflexivity）。在 Crypto 中，Perp 往往引導 Spot 價格。

---

### 📝 寫作語料庫 (Citable Material)

• **定義 (Peg Mechanism)**: "The incentive to receive the funding rate... is sufficient to 'peg' the price of the Perpetual Future to the price of the actual asset even during the interval between funding events."
• **代碼資源**: 附錄包含完整的 R 代碼實現 (Agent, Forecast, Orderbook)，可供我們轉寫為 Python/Rust 進行更復雜的模擬。

---

### 🚀 行動清單 (Action Items)

- [ ] **代碼轉寫**: 將附錄的 R 代碼邏輯移植到我們的 Python `hft_backtest` 或 Rust `sim` 模塊中，用於測試我們的 Funding Rate 套利策略。
- [ ] **加入清算模塊**: 在此模型基礎上增加 "Liquidation Engine"，模擬連環爆倉場景，測試我們的策略在極端波動下的生存能力。
