# Optimal Signal Extraction from Order Flow: A Matched Filter Perspective
ref: 018
arxiv: https://arxiv.org/abs/2512.18648
Authors: Sungwoo Kang (Korea University)
Published: 2025 (December 2025; ArXiv Jan 7, 2026)

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Optimal Signal Extraction from Order Flow: A Matched Filter Perspective on Normalization and Market Microstructure
• **作者**： Sungwoo Kang (Korea University)
• **年份**： 2025 (December 2025; ArXiv Jan 7, 2026)
• **期刊/會議**： ArXiv:2512.18648 [q-fin.CP]
• **引用格式**： Kang, S. (2025). Optimal Signal Extraction from Order Flow: A Matched Filter Perspective. arXiv preprint arXiv:2512.18648.
• **關鍵詞**： #Order_Flow_Imbalance #Signal_Processing #Matched_Filter #Market_Microstructure #Alpha_Research
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Alpha_Factor_Engineering]], [[OFI]], [[Cross_Sectional_Strategies]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 現有的 Order Flow Imbalance (OFI) 指標通常通過除以當日成交量（Volume）來標準化：$OFI_{Vol} = \frac{BuyVol - SellVol}{TotalVol}$。
- 作者認為這種做法是錯誤的，因為它引入了 "Inverse Turnover" 帶來的異方差噪聲（Heteroskedastic Noise）。

• **研究目的**：

- 提出基於信號處理理論的 "Matched Filter" 觀點。
- 證明應該用 **Market Capitalization (市值)** 而非 Volume 來標準化訂單流。
- 驗證 Market Cap Normalization ($S_{MC}$) 在預測未來收益率方面優於 Volume Normalization ($S_{TV}$)。

• **理論框架**：

- **Informed Traders**: 根據資產的 Capacity (Market Cap) 來決定倉位大小 $\to Q_{inf} \propto M_i$。
- **Noise Traders**: 根據當日流動性 (Volume) 來交易 $\to Q_{noise} \propto V_i$。
- **Signal Extraction**: 如果除以 $V_i$，信號部分變成 $\frac{M_i}{V_i} \alpha$，即信號被 "Inverse Turnover" 扭曲。如果除以 $M_i$，信號部分變成常數 $\alpha$，噪聲部分變成 $\frac{V_i}{M_i} \zeta$。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **Matched Filter Theory**:

- 在信號處理中，為了最大化信噪比 (SNR)，濾波器應該匹配信號的結構。
- 因為 Informed Flow 結構與 $M_i$ 成正比，所以最佳濾波器是 $1/M_i$。

• **Monte Carlo Simulation**:

- 模擬了 1000 次市場，包含 500 隻股票。
- 結果顯示 $S_{MC}$ 與未來收益的相關性比 $S_{TV}$ 高 1.32 倍。

• **Empirical Validation**:

- 數據：韓國股市 2.1 百萬個 Stock-Day 樣本 (2020-2024)。
- 方法：Fama-MacBeth 回歸，比較 $S_{MC}$ 和 $S_{TV}$ 對未來收益的預測能力。
- **Horse Race**: 當兩者同時放入回歸時，$S_{MC}$ 係數顯著，$S_{TV}$ 甚至發生符號反轉（變為負），說明 $S_{TV}$ 主要是噪聲。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Superiority of MC Normalization**: $S_{MC} = \frac{\text{Net Buying}}{\text{Market Cap}}$ 在預測收益率上顯著優於 $S_{TV}$ (t-stat 9.65 vs 2.10)。
2. **Small Cap Advantage**: 對於小市值股票（Turnover 差異大），$S_{MC}$ 的優勢最大（2.38倍）。
3. **Turnover is Noise**: 高換手率往往代表高意見分歧（Disagreement），而非信息。除以 Volume 會放大低換手率股票的信號權重（錯誤），壓低高換手率股票的權重。

• **圖表摘要**：

- **Fig 1**: 參數敏感性分析，顯示當 Turnover Range 變大時，$S_{MC}$ 的優勢線性增加。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 這是一個非常 "Engineering" 的洞察。大多數 Quant 習慣性地用 Volume 做分母，卻忽略了其背後的假設。
- 為 Cross-Sectional Alpha Factor 提供了一個簡單而強大的改進方案：**用 Market Cap 替換 Volume 作為分母**。

• **對 HFT 的啟示**：

- **OFI Factor**: 在計算 OFI 因子時，嘗試 $OFI / AvgPrice \times SharesOutstanding$。
- **Aggregated Order Flow**: 對於長時間窗口（如 Daily/Hourly）的信號，Market Cap Normalization 至關重要。對於極短時間窗口（Tick），Volume Normalization 可能仍有意義（因為短期衝擊與 Order Book Depth 有關，而 Depth 與 Volume 相關），但本文觀點值得測試。

---

### 📝 寫作語料庫 (Citable Material)

• **金句**: "Market capitalization normalization acts as a 'matched filter' for informed trading signals. ... Trading value normalization multiplies the signal by inverse turnover—a highly volatile quantity."

---

### 🚀 行動清單 (Action Items)

- [ ] **Alpha Refactoring**: 修改 `research/alphas/<alpha_id>/impl.py`，增加 `OFI_MC` (OFI normalized by Market Cap) 因子。
- [ ] **Empirical Test**: 在我們的數據集上對比 `OFI_Vol` 和 `OFI_MC` 的 IC (Information Coefficient)。
