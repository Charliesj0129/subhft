# Reinforcement Learning in Financial Decision Making: A Systematic Review

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Reinforcement Learning in Financial Decision Making: A Systematic Review of Performance, Challenges, and Implementation Strategies
• **作者**： Mohammad Rezoanul Hoque et al. (University of New Orleans)
• **年份**： 2025 (ArXiv Dec 11, 2025)
• **期刊/會議**： ArXiv:2512.10913 [q-fin.CP]
• **引用格式**： Hoque, M. R., Ferdaus, M. M., & Hassan, M. K. (2025). Reinforcement Learning in Financial Decision Making: A Systematic Review. arXiv preprint arXiv:2512.10913.
• **關鍵詞**： #Reinforcement_Learning #Market_Making #Systematic_Review #Hybrid_Models #Financial_Machine_Learning
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[RL_Agent_Design]], [[Market_Making_Strategy]]

---

### 🎯 主要結論 (Key Findings)

• **Market Making Dominance**: 做市商策略（Market Making）是 RL 在金融領域應用最成功、效果最顯著的領域（相較於 Portfolio Optimization 或 Directional Trading）。
• **Hybrid Models**: 純 RL 模型往往不如 "Hybrid Approaches"（RL + 傳統量化模型）。例如，用傳統模型做基礎（Prior），用 RL 做殘差優化（Residual Learning）或參數動態調整。
• **Implementation over Algorithm**: 成功的關鍵往往在於 "Implementation Quality"（數據預處理、模擬器擬真度、延遲建模），而非算法本身的複雜度（DQN vs PPO）。

---

### 🧠 對 HFT 的啟示 (Implications for HFT)

• **確認方向**: 我們目前專注於 RL Market Making 是正確的。
• **Hybrid Design**: 我們的 `HftEnv` 設計應該包含 "Inventory Constraint" 或 "Avellaneda-Stoikov" 作為 Baseline，讓 RL Agent 學習如何改進它，而不是從零學起。
• **Sim-to-Real**: 文中強調 "Robustness in nonstationary environments"，這再次印證了 Paper 16 的 Regime Switching 觀點。
