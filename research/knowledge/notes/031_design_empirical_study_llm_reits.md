# Design and Empirical Study of a Large Language Model-Based Multi-Agent Investment System for Chinese Public REITs

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Design and Empirical Study of a Large Language Model-Based Multi-Agent Investment System for Chinese Public REITs
• **作者**： Zheng Li (Independent?)
• **年份**： 2026 (January 22, 2026; ArXiv)
• **期刊/會議**： ArXiv:2602.00082 [q-fin.ST]
• **引用格式**： Li, Z. (2026). Design and Empirical Study of a Large Language Model-Based Multi-Agent Investment System for Chinese Public REITs. arXiv preprint arXiv:2602.00082.
• **關鍵詞**： #LLM_Trading #Multi_Agent_System #REITs #DeepSeek_R1 #Qwen3_8B #Fine_Tuning #Sideways_Market
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[LLM_FinTuning]], [[Agentic_Workflow]], [[Chinese_Financial_Markets]]

---

### 🎯 核心設計 (Core Design)

這是一篇非常 "工程化" 且 "落地導向" 的論文，針對 **低波動性 (Low Volatility)** 的中國 REITs 市場設計了一套 Multi-Agent 系統。
• **Four-Agent Analysis Layer**:

1.  **Price Momentum Agent**: 特點是引入了 **Dynamic Volatility Threshold** ($\theta_t$)。在 REITs 這種死氣沈沈的市場，區分 "Sideways" (橫盤) 和 "Trend" 至關重要。必須用動態波動率來定義什麼是 "Effective Breakout"。
2.  **Announcement Agent**: 不只讀新聞，還調用 **Historical Impact Database**。如果今天發布 "分紅公告"，Agent 會去查過去 3 年類似公告發布後 T+3 的漲跌概率。
3.  **Event Agent**: 專注於季報博弈 (Earnings Game) 和運營數據。
4.  **Market Agent (Macro)**: **四象限宏觀模型**。
    - X軸：利率趨勢 (REITs 估值錨)。
    - Y軸：股票市場情緒 (資金蹺蹺板效應)。
    - 用於判斷當前是 "Tailwind" (順風) 還是 "Headwind"。

• **Prediction & Decision Layer**:

- **Prediction**: 輸出 T+1, T+5, T+20 的 Up/Down/Side 概率分佈。
- **Decision**: 將概率映射為離散的倉位調整信號 (e.g., Hold, Buy 20%, Sell 40%)。

---

### 🤖 模型對比 (Model Comparison)

論文比較了兩條路徑：

1.  **DeepSeek-R1 (The Generalist)**: 直接調用強推理大模型。
    - 優點：Reasoning 能力強，對宏觀解讀更深刻。
    - 缺點：成本高，且傾向於保守。
2.  **Qwen3-8B-FT (The Specialist)**: 經過 SFT (Supervised Fine-Tuning) + GSPO (Reinforcement Learning) 的小模型。
    - **Teacher Distillation**: 用 DeepSeek 生成的高質量推理鏈 (CoT) 來教 Qwen。
    - **GSPO Reward**: Reward = $\alpha \cdot \text{Correctness} + \beta \cdot \text{Format}$。
    - 結果：小模型在 **Sharpe Ratio** 和 **Stability** 上反而擊敗了大模型，且推理成本極低。

---

### 📊 實證結果 (Key Results)

• **Market**: 28 隻上市滿 1 年的中國公募 REITs。
• **Performance**:

- **Buy & Hold**: Return 10.69%, Max Drawdown -11.12%.
- **DeepSeek-R1**: Return 15.50%, Max Drawdown -4.09%.
- **Qwen3-8B-FT**: Return 13.75%, Max Drawdown **-3.46%** (最穩).
  • **Pattern**: Multi-Agent 系統最大的價值不在於 "抓暴漲"，而在於 "避暴跌"。在 2025 年的幾次市場回調中，Agent 都成功減倉。

---

### 🧠 HFT 與 Alpha 啟示 (Implications for HFT)

• **Sideways Modeling**: - 在 HFT 中，我們經常只關注波動率放大的時刻。但這篇論文提醒我們，**定義 "Sideways" (無須交易的噪聲區)** 和定義趨勢一樣重要。- **Action**: 我們應該引入類似的動態閾值 $\theta_t = \sigma_t \cdot m_t$ 來過濾 HFT 的開倉信號。如果預測收益 $|E[r]| < \theta_t$，則視為 Sideways，不交易，節省手續費。

• **Small Model Distillation**: - HFT 對延遲極其敏感，不可能實時調用 GPT-4。- 這篇論文證明了：**可以用大模型 (GPT-4/DeepSeek) 生成高質量的標註數據 (Silver Labels)，然後蒸餾給小模型 (Bert/TinyLlama)**。此路徑在 HFT 信號生成中完全可行。- 我們可以用 DeepSeek 分析 Order Book Heatmap 生成 "解讀"，然後訓練一個 CNN/Transformer 小模型去模仿這個解讀。

• **Macro Context**: - "利率 vs 股市" 的四象限模型非常直觀。對於我們做 Crypto HFT，可以建立類似的 **"BTC 波動率 vs Funding Rate"** 四象限模型，作為 Global State 輸入給 RL Agent。

---

### 🚀 行動清單 (Action Items)

- [ ] **Sideways Filter**: 在我們的 `MarketDataNormalizer` 中計算動態波動率閾值，作為一個 Feature 傳給策略。
- [ ] **Distillation Pipeline**: 嘗試用 DeepSeek R1 對我們的歷史回測中的 "大虧單" 進行文字分析 (Post-Mortem)，生成 "為什麼會虧" 的解釋，然後嘗試用這些解釋來優化我們的 Risk Model。
