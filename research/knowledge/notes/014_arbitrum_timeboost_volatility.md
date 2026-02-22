# Impact of Volatility on Time-Based Transaction Ordering Policies (Arbitrum Timeboost)

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Impact of Volatility on Time-Based Transaction Ordering Policies
• **作者**： Ko Sunghun, Jinsuk Park (Matroos Labs & KAIST)
• **年份**： 2025 (December 2025; ArXiv Dec 29, 2025)
• **期刊/會議**： ArXiv:2512.23386 [cs.GT]
• **引用格式**： Sunghun, K., & Park, J. (2025). Impact of Volatility on Time-Based Transaction Ordering Policies. arXiv preprint arXiv:2512.23386.
• **關鍵詞**： #Timeboost #Arbitrum #MEV #Express_Lane #Transaction_Ordering #Volatility_Risk_Premium
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Arbitrum_Sequencer]], [[MEV_Auctions]], [[CEX_DEX_Arbitrage]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- Arbitrum 引入了 **Timeboost** 機制（Express Lane Auction, ELA），允許贏家獲得 200ms 的獨家延遲優勢。
- 這是一個全新的 "Time-Based Ordering" 機制，類似於 TradFi 的 "Speed Bumps/Lanes"。
- 目前尚無實證研究分析投標者（HFT Searchers）如何對這一特權進行定價。

• **研究目的**：

- 分析 Timeboost 的 ELA 數據（實際 bids）。
- 驗證假設：HFT 對 Express Lane 的估值低於理論上的風險中性價值（Expected CEX-DEX Arb Profit）。
- 將這種 "Discount" 歸因於 **Variance Risk Premium (VRP)**：預測短時（1分鐘）波動率的難度極大，導致風險厭惡的 Searchers 降低出價。

• **理論框架**：

- **Valuation Model**: $v_{ir} = \alpha + \beta E[IV] - \gamma Var(IV)$.
- **Mechanism**: Second-Price Sealed-Bid Auction.

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **Data Analysis**:

- 分析了 2025 年 5 月至 10 月的 Arbitrum ELA 數據。
- 主要投標者：地址 `0x8c6f` 和 `0x95c0`（控制了 90% 的勝率）。
- **Volatility Proxy**: 使用 Binance US ETH/USDT 高頻數據計算 Realized Volatility ($RV$) 作為 $E[IV]$ 的代理。

• **Regression Model**:

- 使用 Tobit 模型（因為 Bids 有下限 Reserve Price）迴歸 Bid Amount 與 $E[IV]$ 和 $Var(IV)$ 的關係。
- **Result**: $\theta_1 > 0$ (Bid 隨預期波動率增加), $\theta_2 < 0$ (Bid 隨預測不確定性減少)。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Undervaluation**: Searchers 出價顯著低於理論利潤。這是由於 "Forecast Risk"（無法準確預測下一分鐘是否有足夠的波動率來覆蓋成本）。
2. **Market Dominance**: 少數幾個玩家主導了市場，這可能導致合謀或寡頭壟斷，進一步壓低價格。
3. **Auction Inefficiency**: 由於 VRP 的存在，Arbitrum DAO 可能未能捕獲全部的 MEV 價值（Searchers 留下了大部分利潤）。

• **圖表摘要**：

- **Fig 1**: Timeboost 累積收入（超過 1400 ETH），證明了其商業上的成功。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 提供了 Arbitrum Timeboost 的定價模型。
- 揭示了 HFT 在拍賣中的風險厭惡行為。

• **對 HFT 的啟示**：

- **Timeboost Strategy**: 如果我們想在 Arbitrum 上做 CEX-DEX Arb，我們必須參與 Timeboost。
- **Bidding Strategy**: 建立一個預測模型 $E[IV_{t+1min}]$。如果我們的預測值高於當前市場 Winning Bid 的隱含 IV，我們就應該出價。
- **Opportunity**: 由於市場存在 "Discount"（因風險厭惡），如果我們的波動率預測模型更準確（Alpha），我們就能以便宜的價格買到 Timeboost 權限，從而獲得超額利潤。

---

### 📝 寫作語料庫 (Citable Material)

• **觀察**: "Bids are significantly discounted relative to risk-neutral valuation... consistent with variance risk premium."
• **機制**: "Transactions submitted via the normal lane incur a 200-millisecond delay... EL immediately forwards... offering a 200ms latency advantage."

---

### 🚀 行動清單 (Action Items)

- [ ] **Arbitrum ELA Monitor**: 部署一個腳本監控 Arbitrum Timeboost 拍賣的實時 Bids。
- [ ] **IV Prediction Model**: 訓練一個專門針對 1 分鐘級別波動率的預測模型（使用 Order Book Imbalance 作為特徵，往往領先波動率）。
