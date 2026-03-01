# Autonomous Market Intelligence: Agentic AI Nowcasting Predicts Stock Returns
ref: 028
arxiv: https://arxiv.org/abs/2601.11958
Authors: Zefeng Chen, Darcy Pu (Peking University)
Published: 2026 (January 2026; ArXiv Jan 17, 2026)

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Autonomous Market Intelligence: Agentic AI Nowcasting Predicts Stock Returns
• **作者**： Zefeng Chen, Darcy Pu (Peking University)
• **年份**： 2026 (January 2026; ArXiv Jan 17, 2026)
• **期刊/會議**： ArXiv:2601.11958 [q-fin.GN]
• **引用格式**： Chen, Z., & Pu, D. (2026). Autonomous Market Intelligence: Agentic AI Nowcasting Predicts Stock Returns. arXiv preprint arXiv:2601.11958.
• **關鍵詞**： #Agentic_AI #LLM #Stock_Prediction #Nowcasting #Market_Efficiency #Look-Ahead_Bias #Asymmetric_Predictability
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[LLM_Trading]], [[Sentiment_Analysis]], [[Alpha_Generation]]

---

### 🎯 核心發現 (Core Findings)

• **Real Predictive Power**: 在完全沒有人類篩選信息的情況下（Agentic Mode），LLM 能夠通過自主搜索互聯網信息，預測股票收益。
• **Symmetry Breaking (Asymmetry)**:

- **Winners**: AI 非常擅長挑選 Top 20 贏家。Top-20 Portfolio 產生了 **18.4 bps daily alpha** (annualized Sharpe 2.43)。
- **Losers**: AI 無法區分輸家和普通股票。Bottom Portfolio 的 Alpha 與零無異。
- **Reasoning**: 正面消息（EARNINGS BEAT, PRODUCT LAUNCH）通常是清晰且一致的。負面消息則充滿了公司公關的混淆視聽（Obfuscation）和社交媒體的噪音（"Buy the dip"）。AI 訓練數據中可能充滿了對負面消息的 "Euphemisms"（委婉語），導致其識別能力較弱。

---

### 🛠 方法論 (Methodology)

• **Universe**: Russell 1000 (Liquid Large Caps).
• **Timing**: Daily Rank. 每天收盤後生成信號，第二天開盤執行（Open-to-Open return），嚴格避免 Look-ahead Bias。
• **Agentic Prompting**:

- 不給 AI 餵新聞。
- 而是給 AI 一個 Prompt："Evaluate the attractiveness of [Stock]... Go search the web."
- AI 自主決定搜索什麼關鍵詞、閱讀什麼鏈接。
  • **Signals**:
- `Attractiveness Score` (-5 to +5).
- `Market Sentiment` / `Divergence`.
- `Fundamental Forecasts` (EPS, Price Target).

---

### 📊 策略特徵 (Strategy Profile)

• **Factor Exposure**:

- **Low Beta**: AI 傾向於選低 Beta 股票。
- **Growth Bias**: 強烈的 Growth 風格（Negative HML loading）。
- **Size Bias**: 偏好超大盤股（Negative SMB loading）。
- **Momentum**: 對 Momentum 因子暴露不顯著，說明不是簡單的追漲。
  • **Turnover**: 日頻換倉，但在 Liquid Universe (Russell 1000) 中，Transaction Cost < 10% of Gross Alpha。

---

### 🧠 HFT 與 Alpha 啟示 (Implications for HFT & Alpha)

• **New Alpha Source**:

- 這種 Alpha 來自於 "Information Synthesis" 而非 "Speed"。
- 傳統 HFT 拼速度，Quant 拼因子挖掘。Agentic AI 拼的是 "閱讀理解和信息整合的廣度"。
- 這開啟了一個新的 Alpha 類別：**Semantic Alpha**。

• **Execution**:

- 雖然論文做的是 Daily Rebalancing，但這個信號可以作為 HFT 的 **Contextual Bias**。
- 如果 AI 給出 Strong Buy (+5)，HFT 策略在當天應該傾向於 Passive Buy 或 Aggressive Buy，而在 Sell side 應該更保守。

• **Productionization**:

- 運行 LLM Agent 成本高且慢。如何將其 "Distill" 成一個低延遲的信號是關鍵。
- 可能的路徑：用大模型（如 GPT-4/Claude-3.5）生成 Daily Context，然後用小模型（Bert/RoBERTa）實時處理新聞流並與 Context 對齊。

---

### 🚀 行動清單 (Action Items)

- [ ] **Replication**: 嘗試復現這個 Pipeline。使用 Perplexity API 或 Google Search API + GPT-4o。
  - Target: 選 50 個流動性最好的 Crypto Assets，每天生成 Attractiveness Score。
- [ ] **Signal Integration**: 將 `AI_Attractiveness_Score` 作為一個低頻特徵加入到我們的 RL Agent 狀態空間中。
