# A Test of Lookahead Bias in LLM Forecasts

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： A Test of Lookahead Bias in LLM Forecasts
• **作者**： Zhenyu Gao, Wenxi Jiang, Yutong Yan (CUHK Business School)
• **年份**： 2026 (January 1, 2026; ArXiv Dec 2025)
• **期刊/會議**： ArXiv:2512.23847
• **引用格式**： Gao, Z., Jiang, W., & Yan, Y. (2025). A Test of Lookahead Bias in LLM Forecasts. arXiv preprint arXiv:2512.23847.
• **關鍵詞**： #LLM_Forecasting #Lookahead_Bias #Membership_Inference_Attack #LAP #Financial_NLP
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Alpha_Screening]], [[LLM_in_Finance]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 最近大量研究聲稱 LLM 在金融預測（如股票回報、盈利預測）上超越了傳統模型。
- 然而，這些研究大多使用了 Pre-trained LLM 進行 In-sample 評估。由於 LLM 的訓練數據（如 Common Crawl）包含了大量歷史財經新聞和隨後的市場反應，LLM 可能只是在 "Recall"（回憶）而非 "Reason"（推理）。
- 現有的去重方法（如 mask 實體名、時間）效果有限，且缺乏系統性的統計檢驗方法。

• **研究目的**：

- 提出一個統計檢驗量：**Lookahead Propensity (LAP)**。
- 使用 LAP 來量化 LLM 對特定 Prompt 的「熟悉度」（即該文本是否出現在訓練集中）。
- 驗證 LLM 的預測能力是否源於 Lookahead Bias：如果預測準確率與 LAP 呈正相關，則存在偏誤。

• **理論框架**：

- **Membership Inference Attack (MIA)**: 借用隱私安全領域的技術，通過 token probability 判斷樣本是否在訓練集中。
- **Min-K% Prob**: 取 Prompt 中概率最低的 K% token 的平均對數概率作為 LAP 指標。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **Lookahead Propensity (LAP)**：

- 定義：$LAP(w, K) = \exp \left( \frac{1}{|S_K|} \sum_{t \in S_K} \log P_\theta(w_t | w_{<t}) \right)$
- 核心思想：如果一個 Prompt 曾經出現在訓練數據中，LLM 對其中通常「低概率」（生僻）的詞的預測概率會異常地高。因此，關注 Bottom-K% 的 token probability 能有效區分 Seen vs Unseen 文本。
- 設定 $K=20\%$。

• **計量模型**：

- 回歸方程：$R_{t+1} = \alpha + \beta_{LLM} \cdot \text{Signal}_{LLM} + \delta \cdot (\text{Signal}_{LLM} \times LAP) + \dots$
- 關鍵假設：如果 $\delta > 0$，說明 LLM 在「熟悉」樣本上的預測力更強，即存在 Lookahead Bias。

• **實驗設置**：

- **Task 1**: Headlines -> Stock Returns (Lopez-Lira & Tang 2023 復現)。
- **Task 2**: Earnings Transcripts -> CapEx Prediction (Jha et al. 2024 復現)。
- **Model**: Llama-3.3 (Dec 2024 released)，並用 Llama-2 作為 Out-of-sample placebo test。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **顯著的偏誤**：在 In-sample 測試中，交互項 $\text{Signal}_{LLM} \times LAP$ 顯著為正。
   - 對於股票預測，一個標準差的 LAP 增加會使 LLM 預測的邊際效果增加 37%。
   - 這意味著 LLM 的「超額收益」很大一部分來自於它「看過」這條新聞及其後的市場反應。
2. **小市值效應**：之前文獻發現 LLM 在小市值股票上表現更好，但本文發現這實際上是因為小市值股票的 Lookahead Bias 更嚴重（可能是小股票新聞較少，一旦被 LLM 記住，回憶效果更精準）。
3. **Out-of-Sample Placebo**：使用 2023 年發布的 Llama-2 測試 2024 年的數據（真正的未知未來），交互項變為不顯著，說明沒有 Lookahead Bias 時，LAP 不會提升預測力。

• **圖表摘要**：

- **Table II**: 顯示加入 LAP 交互項後，原本的 LLM Signal 係數變得不顯著或大幅下降，證明之前的顯著性多半是假象。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 這是對 "LLM for Alpha" 領域的一個重大打擊（Reality Check）。它提供了一個低成本的診斷工具（LAP），不需要重新訓練模型就能檢測偏誤。
- 證明了許多所謂的 "Emergent Reasoning Capabilities" 在金融領域可能只是 "Stochastic Parrots" 的記憶回涌。

• **對 HFT 的啟示**：

- **Alpha Screening**: 我們在使用 LLM 挖掘 Alpha 時，必須計算 LAP。如果我們發現某個 Alpha 在高 LAP 樣本上表現極好，但在低 LAP 樣本上失效，那麼這個 Alpha 是假的。
- **Prompt Engineering**: 我們應該嘗試通過 Rewrite Prompt（改寫新聞結構、同義詞替換）來降低 LAP，強制 LLM 進行推理而不是回憶。

---

### 📝 寫作語料庫 (Citable Material)

• **定義 (Lookahead Bias Contamination)**: "A positive correlation between LAP and forecast accuracy indicates... lookahead bias."
• **警語**: "Ideally, one would evaluate forecasts strictly on out-of-sample data... however... the available out-of-sample horizon is short."

---

### 🚀 行動清單 (Action Items)

- [ ] **實現 LAP 工具**: 在 `hft_backtest` 中集成一個 LAP 計算模塊（調用 LLM 獲取 logprobs）。
- [ ] **審計現有 Alpha**: 對我們之前用 LLM 生成的 Sentiment Alpha 進行 LAP 回歸測試，看看有多少是真實的。
