# DeePM: Regime-Robust Deep Learning for Systematic Macro Portfolio Management

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： DeePM: Regime-Robust Deep Learning for Systematic Macro Portfolio Management
• **作者**： Kieran Wood, Stephen J. Roberts, Stefan Zohren (University of Oxford, Oxford-Man Institute)
• **年份**： 2026 (January 12, 2026; ArXiv)
• **期刊/會議**： ArXiv:2601.05975 [q-fin.TR]
• **引用格式**： Wood, K., Roberts, S. J., & Zohren, S. (2026). DeePM: Regime-Robust Deep Learning for Systematic Macro Portfolio Management. arXiv preprint arXiv:2601.05975.
• **關鍵詞**： #Deep_Learning_Portfolio #Graph_Neural_Networks #Macro_Trading #Robust_Optimization #Causal_Sieve #Entropic_VaR
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Momentum_Transformer]], [[Graph_Neural_Networks]], [[Portfolio_Optimization]], [[Systematic_Macro]]

---

### 🎯 核心創新 (Core Innovations)

1.  **Directed Delay (Causal Sieve)**:
    - **Problem**: 全球市場收盤時間不同步（Asynchronous Closes）。如果直接用同一天的 `Close` 進行 Cross-Attention，會引入 Look-ahead Bias（例如用收盤較晚的美股信息去預測已經收盤的亞股）。
    - **Solution**: 強制 Cross-Sectional Layer 使用 $t-1$ 的信息。這看似犧牲了時效性，但實際上過濾了 "Spurious Intraday Correlation"，強迫模型學習真正的 "Causal Impulse-Response"（因果脈衝響應）。

2.  **Macro Graph Prior (GNN Regularization)**:
    - **Problem**: 純數據驅動的 Cross-Attention 在低信噪比下容易 Overfit 到隨機相關性。
    - **Solution**: 引入一個 **Fixed Economic Graph**（基於供應鏈、板塊、Carry Channel 等經濟學原理構建的稀疏矩陣）。
    - **Mechanism**: 使用 GAT (Graph Attention Network) 將此圖作為 Prior。在信噪比低時，模型退化為遵循經濟學常識（如原油上漲 -> 能源股上漲）；只有在數據信號極強時才允許偏離 Prior。

3.  **Differentiable EVaR Objective (SoftMin)**:
    - **Problem**: 最大化 Sharpe Ratio 會導致策略在 "Lucky Windows" 過擬合，而在 "Crisis" 中崩潰。
    - **Solution**: 使用 **SoftMin of Rolling Sharpe Ratios** 作為 Loss Function。這等價於優化 Entropic Value-at-Risk (EVaR)，迫使模型在此刻優化 "歷史上最差的那個窗口" 的表現（Minimax）。

---

### 🛠 模型架構 (Architecture)

• **Pipeline**:

1.  **Temporal Encoder**: `V-VSN` (Feature Selection) -> `LSTM` (Local Denoising) -> `Temporal Attention` (Global Context).
    - _Insight_: LSTM 處理高頻噪聲，Attention 捕捉長期 Regime。
2.  **Cross-Sectional Interaction**: `Directed Delay Attention`.
3.  **Structural Regularization**: `Macro Graph GAT`.
4.  **Action Head**: `tanh` 輸出目標權重，並直接優化 **Net-of-Cost Returns**。

---

### 📊 實驗結果 (Empirical Evidence)

• **Data**: 50 Liquid Futures (Commodities, FX, Equities, Rates) 2010-2025.
• **Performance**:

- Net Risk-Adjusted Return 是 Momentum Transformer 的 **1.5倍**。
- Maximum Drawdown 減少了 **21%** (歸功於 Graph Prior)。
- 跨越了 "CTA Winter" (2010s) 和 "Covid/Inflation" (2020s) 兩個截然不同的 Regime，證明了結構的魯棒性。

---

### 🧠 HFT 與 Alpha 啟示 (Implications for HFT)

• **Asynchrony Handling**:
_ 在 HFT 中，不同交易所的 Latency 不同。我們在訓練模型時，必須像 DeePM 一樣嚴格處理 "Event Time"，不能簡單地用 "Wall Clock Time" 對齊Ｋ線，否則會有 Look-ahead Bias。
_ **Action**: 檢查我們的 Data Loader 是否在 Cross-Exchange Arbitrage 訓練中正確處理了時間戳對齊。

• **Structural Priors**:
_ 純 End-to-End Learning 在金融數據上極難成功。DeePM 證明了 **Inductive Bias** (Domain Knowledge) 的重要性。
_ 對於 HFT，我們的 "Graph Prior" 可以是 **Order Book Imbalance -> Price Change** 的物理機制，或者是 **Binance -> Uniswap** 的搬磚機制。不要讓模型自己去猜這些關係，直接把這些結構寫進模型（作為 Mask 或 Lag）。

• **Robust Objective**: \* 不要只優化 PnL 或 Sharpe。嘗試在 RL Reward 中加入類似 **SoftMin** 的懲罰項，專注於提升 "最差的一分鐘" 的表現，這能顯著提高實盤的生存率。

---

### 🚀 行動清單 (Action Items)

- [ ] **Objective Function**: 修改我們 RL Agent 的 Reward Function，從 `Mean(PnL)` 改為 `SoftMin(Rolling_Sharpe)`，測試是否能減少 Drawdown。
- [ ] **Graph Prior**: 在我們的 `Multi-Asset Alpha` 模型中，嘗試構建一個基於 "Correlation Matrix" 或 "Sector" 的 Graph Mask，限制 Attention 的範圍。
