# Alpha-R1: Alpha Screening with LLM Reasoning via Reinforcement Learning

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Alpha-R1: Alpha Screening with LLM Reasoning via Reinforcement Learning
• **作者**： Zuoyou Jiang, Li Zhao et al. (Shanghai Jiao Tong University & StepFun)
• **年份**： 2025 (ArXiv 2025/12/29)
• **期刊/會議**： ArXiv:2512.23515
• **引用格式**： Jiang, Z., Zhao, L., et al. (2025). Alpha-R1: Alpha Screening with LLM Reasoning via Reinforcement Learning. arXiv preprint arXiv:2512.23515.
• **關鍵詞**： #Alpha_Screening #Reasoning_LLM #Reinforcement_Learning #GRPO #Regime_Aware
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[DeepSeek_R1_Architecture]], [[Factor_Zoo_Screening]], [[RLHF_in_Finance]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- **Non-stationarity**: 金融市場是非平穩的，因子表現會隨體制（Regime）變化（例如動量因子在震蕩市失效）。
- **Traditional ML limits**: 傳統 ML（如 XGBoost, Lasso）只能基於歷史相關性進行靜態或滾動預測，對 "Regime Shift" 反應滯後。
- **LLM limits**: 通用 LLM 缺乏金融定價觀（Alignment），且通常只用於 "Mining"（挖掘因子）而非 "Screening"（動態篩選）。

• **研究目的**：

- 提出 **Alpha-R1**：一個專門用於動態因子篩選（Alpha Screening）的 Reasoning LLM（8B 參數）。
- 利用 **Reinforcement Learning (RL)** 訓練 LLM，使其具備「根據當前市場狀態，推理出哪些因子應該生效」的能力。
- 使用 **GRPO** (Group Relative Policy Optimization) 替代傳統 PPO，無需 Value Network，更適合推理任務。

• **理論框架**：

- **Context-Conditioned Gating**: 將 LLM 視為一個語義門控網絡（Semantic Gating Network），根據（市場語義 + 因子語義）決定因子開關。
- **Reinforcement Learning from Market Feedback (RLMF)**: 用真實的回測績效（Sharpe, Ret）作為 Reward 信號。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **Alpha-R1 Framework**：

1. **Data Abstraction (數據抽象)**:
   - **Market State ($S_t$)**: 將技術指標、宏觀新聞轉化為「文本描述」（例如 "Market is in reliable uptrend with low vol"）。
   - **Factor Semantics ($\alpha_{des}$)**: 將數學因子（如 `ts_rank(close, 10)`）轉化為語義描述（"Short-term momentum factor"）。
2. **Reasoning Core (推理核心)**:
   - 輸入：$S_t \oplus \{\alpha_{des, i}\}$
   - 輸出：$A_t$ (Selected Factors List) 及其推理過程（Chain of Thought）。
   - LLM 需要解釋 "Why I choose Momentum now?"（因為市場處於趨勢中...）。
3. **RL Optimization (GRPO)**:
   - **Reward**: $R_{final} = R_{adjusted} - P_{structural}$
   - $R_{adjusted}$: 基於未來 5 天的組合超額收益（線性權重組合選中的因子）。
   - $P_{structural}$: 結構性懲罰（保證選出的因子是合法的、稀疏的）。
   - **Critic-Free**: 使用 GRPO，通過一組採樣（Group Sampling）的相對優劣來計算 Advantage，無需訓練 Critic 模型，極大降低顯存需求。

• **數據集**：

- **Factor Zoo**: 從 Alpha101 中篩選出 82 個因子。
- **Backbone**: Qwen3-8B。
- **Period**: 2020-2023 Pre-train, 2024 Train, 2025 Test.

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **SOTA Performance**: Alpha-R1 在 CSI 300 上達到 **Sharpe 1.62**，遠超傳統 RL (PPO: 0.11, A2C: -0.85) 和通用 Reasoning Model (DeepSeek-R1: -0.82)。
2. **Generalization**: 在未見過的 CSI 1000（小票）上 Zero-shot 表現更是驚人（Sharpe 4.03），證明了 "Reasoning"（理解因子邏輯）比 "Pattern Matching"（擬合歷史數據）具有更強的遷移能力。
3. **Ablation Study**: 去掉 "News" 或 "Semantic Description" 都會導致性能顯著下降，證明了多模態（文本+時序）融合的必要性。

• **關鍵洞察**:

- 通用推理模型（如 DeepSeek-R1, Claude 3.5 Sonnet）做交易效果很差，因為它們沒有與「金融目標函數」（Sharpe Ratio）對齊，只會泛泛而談。必須經過 RL 微調。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- **Methodological Breakthrough**: 這解釋了如何正確使用 LLM 進行量化交易——不是讓它直接預測股價（那會有 Lookahead Bias 且噪聲大），而是讓它做 **Meta-Learning**（根據環境選擇專家/因子）。
- **RLMF Paradigm**: 提出了一種用市場反饋替代人類反饋（RLHF）的可行路徑。

• **對 HFT 的啟示**：

- 我們的 HFT 系統目前是靜態的（固定策略參數）。我們應該實現一個 **Mini-Alpha-R1**。
- 我們不需要訓練一個 8B 模型，可以用小的 LLM 甚至 Prompt Engineering，輸入「當前 Order Book 狀態描述」和「策略參數描述」，讓它選擇參數。
- **Immediate Task**: 我們手頭有 papers 3, 4, 5, 6，它們都是特定的策略/因子。Alpha-R1 的架構告訴我們如何將這些獨立的論文（因子）整合起來——通過一個 Reasoning Layer 在不同市場狀態下動態切換它們。

---

### 📝 寫作語料庫 (Citable Material)

• **架構定義**: "It inductively reasons over heterogeneous market information to assess the economic relevance of candidate factors... serving as the system's cognitive core."
• **優勢描述**: "Delegating non-stationarity adaptation to the reasoning core allows the system to navigate regime shifts without the instability of purely numerical re-estimation."

---

### 🚀 行動清單 (Action Items)

- [ ] **設計 Reasoning Gating**: 模仿 Alpha-R1，設計一個簡單的 "Strategy Selector"。
  - 輸入：最近 1 小時的 Volatility, Spread, Order Book Imbalance。
  - 候選策略：Paper 2 (Basis Trading), Paper 3 (Trajectory Opt), Paper 5 (Funding Arb).
  - 任務：讓 LLM 輸出當前應該激活哪個策略。
- [ ] **準備語義描述**: 為我們實現的每個策略寫一段清晰的 "Semantic Description"（例如： "This strategy profits from mean-reversion in high vol settings..."）。
