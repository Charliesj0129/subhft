# Sources and Nonlinearity of High Volume Return Premium

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Sources and Nonlinearity of High Volume Return Premium: An Empirical Study on the Differential Effects of Investor Identity versus Trading Intensity
• **作者**： Sungwoo Kang (Korea University)
• **年份**： 2025 (ArXiv Dec 24, 2025)
• **期刊/會議**： ArXiv:2512.14134 [q-fin.TR]
• **引用格式**： Kang, S. (2025). Sources and Nonlinearity of High Volume Return Premium. arXiv preprint arXiv:2512.14134.
• **關鍵詞**： #High_Volume_Return_Premium #Investor_Heterogeneity #Market_Cap_Normalization #Institutional_Flow #Korea_Market
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Paper_18]], [[Alpha_Factor_Engineering]], [[Market_Structure]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Puzzle**: 2019 年的研究發現韓國市場存在 "Low Volume Return Premium" (LVRP)，這與全球發現的 "High Volume Return Premium" (HVRP) 相反。
• **Resolution**: 作者證明這個悖論是因為錯誤的測量方法導致的。

1. **Pooling**: 將機構投資者和散戶混合在一起。
2. **Normalization**: 使用 Volume (Trading Value) 進行標準化，而不是 Market Cap。

---

### 🛠 核心發現 (Core Findings)

• **Validation of Market Cap Normalization**:

- 本文使用完全不同的數據集和研究問題，獨立驗證了 [[Paper_18]] 的理論。
- 當使用 **Market Cap** 標準化機構買入強度時，呈現完美的單調性：$Q4 (+12.12\%) > Q3 > Q2 > Q1 (+4.65\%)$。
- 當使用 **Trading Value (Volume)** 標準化時，這種單調性完全消失（$Q2 > Q4$），導致信號失效。

• **Investor Heterogeneity**:

- **Institutions**: Informed. 強度與未來收益正相關（需正確標準化）。
- **Retail**: Noise. 整體收益曲線平坦（Flat），無論買入強度如何，未來收益都接近零。
- **Exception**: 在 "Donghak Ant Movement"（散戶抱團期間），散戶暫時充當了 Liquidity Provider。

---

### 🧠 深度評析 & 綜合 (Synthesis)

• **Cross-Verification**: Paper 18 和 Paper 20 由同一作者（或同一團隊）在不同預印本中提出，互為印證。Paper 18 側重信號處理理論，Paper 20 側重資產定價實證。兩者共同確立了 **"Market Cap Normalization is the Matched Filter for Informed Flow"** 這一核心論點。

• **Actionable Insight**:

- 在構建我們的 "Smart Money" 因子時，必須區分 Investor ID（如果有數據）。
- 即使沒有 Investor ID，在使用總體 Order Flow 時，**必須**嘗試 Market Cap Normalization。
