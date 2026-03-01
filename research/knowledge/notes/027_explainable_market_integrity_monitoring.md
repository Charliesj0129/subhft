# Explainable Market Integrity Monitoring via Multi-Source Attention Signals and Transparent Scoring
ref: 027
arxiv: https://arxiv.org/abs/2601.15304
Authors: Sandeep Neela (Independent Researcher)
Published: 2026 (January 10, 2026)

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： AIMM-X: An Explainable Market Integrity Monitoring System Using Multi-Source Attention Signals and Transparent Scoring
• **作者**： Sandeep Neela (Independent Researcher)
• **年份**： 2026 (January 10, 2026)
• **期刊/會議**： ArXiv:2601.15304 [q-fin.RM]
• **引用格式**： Neela, S. (2026). AIMM-X: An Explainable Market Integrity Monitoring System Using Multi-Source Attention Signals and Transparent Scoring. arXiv preprint arXiv:2601.15304.
• **關鍵詞**： #Market_Integrity #Surveillance #Explainable_AI #Attention_Signals #Meme_Stocks #Triage
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Market_Manipulation_Detection]], [[Social_Sentiment_Analysis]], [[Regulatory_Compliance]]

---

### 🎯 研究目標與方法 (Objectives & Methods)

• **Problem**: 現有的市場監控系統依賴專有的 Order Book 數據（L3），對學術界和小型機構不可見（Black-box）。且缺乏可解釋性，難以在合規審查中提供證據。
• **Solution (AIMM-X)**:

- **Data**: 僅使用公開數據（OHLCV）+ 多源注意力信號（Reddit, StockTwits, Google Trends, Wikipedia, News）。
- **Approach**: 檢測 "Suspicious Windows"（可疑時間窗口），即價格、波動率和注意力同時異常的時期。
- **Philosophy**: "Triage, not Accusation"。系統的目標不是定罪，而是篩選出值得人類專家深入調查的事件。

---

### 🛠 系統架構 (System Architecture)

• **1. Feature Engineering**:

- 計算 Returns, Realized Volatility, 和 Attention Composite Score (加權融合)。
- 使用 Rolling Baseline ($B=20$ days) 計算 Z-scores。

• **2. Detection Logic**:

- **Composite Strength Score**: $s_{i,t} = |z_r| + z_\sigma + z_A$。
- **Hysteresis Thresholding**: 使用雙閾值（$\theta_{high}=3.0, \theta_{low}=2.0$）來確定異常窗口的開始與結束，避免碎片化。

• **3. Interpretable Scoring ($\Phi$ Factors)**:

- $\phi_1$ (Return Shock): 價格劇烈波動。
- $\phi_2$ (Volatility Anomaly): 價格未變但波動率極高（Churning）。
- $\phi_3$ (Attention Spike): 社交熱度激增。
- $\phi_4$ (Co-movement): 價格與注意力的相關性（Coordinated Attack?）。
- $\phi_5$ (Recurrence): 短期內重複發生的異常。
- $\phi_6$ (Disagreement Penalty): 不同注意力源之間的矛盾（防止單一平台 Gaming）。

---

### 📊 實驗結果 (Experimental Results)

• **Scope**: 2024 全年數據，24 個高關注度 Tickers (GME, AMC, META, NVDA, MSTR, COIN 等)。
• **Results**: 檢測到 233 個可疑窗口。
• **Case Studies**:

- GME/AMC: 雖然價格沒有 2021 年那麼誇張，但系統成功捕捉到了由 Reddit 驅動的迷你波動。
- META/NVDA: 捕捉到了 Earnings 相關的異常，這是預期中的 False Positive（或說 Legitimate Volatility），系統設計上依靠人工過濾這些已知事件。

---

### 🧠 HFT 與合規啟示 (Implications for HFT & Compliance)

• **Compliance as α**:

- 作為 HFT，我們不希望被交易所或監管機構標記為 Manipulator。
- 我們可以在內部運行類似 AIMM-X 的系統作為 **"Pre-Compliance Check"**。如果我們的策略導致某個 Ticker 的 Integrity Score 飆升，我們應該自動暫停該策略。

• **Signal Construction**:

- 論文證明了僅用 OHLCV + Attention 就能捕捉大部分異常。這意味著我們不需要昂貴的 L3 數據就能做初步的風控。
- 注意力信號（特別是 Wikipedia 和 Google Trends）比單純的 Twitter Sentiment 更難被偽造，是很好的 Filter。

• **Adversarial Thinking**:

- 懂得監管如何監控（Z-score + Hysteresis），可以幫助我們設計更隱蔽的執行算法（如使衝擊維持在 2.0 sigma 以下，或者在 Baseline 高的時候交易）。_註：此為紅隊測試思維，非建議違規。_

---

### 🚀 行動清單 (Action Items)

- [ ] **Internal Surveillance**: 在我們的回測系統中加入類似的 Integrity Score 計算。任何回測策略如果產生過高的 Integrity Risk Score，需由風險委員會審核。
- [ ] **Data Feed**: 接入 Wikipedia Page Views API（通常是免費的）作為低頻的注意力過濾器。
