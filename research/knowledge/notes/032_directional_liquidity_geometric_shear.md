# Directional Liquidity and Geometric Shear in Pregeometric Order Books

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Directional Liquidity and Geometric Shear in Pregeometric Order Books
• **作者**： João P. da Cruz (The Quantum Computer Company)
• **年份**： 2026 (January 28, 2026; ArXiv)
• **期刊/會議**： ArXiv:2601.19369 [q-fin.TR]
• **引用格式**： da Cruz, J. P. (2026). Directional Liquidity and Geometric Shear in Pregeometric Order Books. arXiv preprint arXiv:2601.19369.
• **關鍵詞**： #Order_Book_Geometry #Pregeometric_Models #Geometric_Shear #Liquidity_Shape #Physics_of_Markets #Gamma_Distribution
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Market_Microstructure]], [[Order_Book_Dynamics]], [[Econophysics]]

---

### 🎯 核心理論 (Core Theory)

這篇論文提出了一個非常抽象且物理學導向的 "Pregeometric" 理論，重新定義了 Order Book 的本質。
• **Pregeometric Substrate**: 市場底層不是價格和時間，而是一個無度量的 "Relational Substrate" (關係基質)。
• **Observable Projection**: 價格 ($p$) 和 流動性 ($\nu$) 只是這個基質在觀察者視角下的投影。
• **Shear vs. Drift**: - **Mid-Price Drift ($m_t$)**: 被定義為 "Gauge Degree of Freedom" (規範自由度)。也就是說，價格的移動只是坐標系的平移，並不改變系統的內在幾何結構。- **Geometric Shear ($\tilde{\rho}_t$)**: 被定義為 "Physical Degree of Freedom" (物理自由度)。Order Book 形狀的扭曲（如 Bid 變厚 Ask 變薄）是系統內在張力的體現。

---

### 📉 經驗發現 (Empirical Findings)

• **Decoupling of Shear and Drift**: - 傳統觀點認為：Order Imbalance (Shear) 推動 Price (Drift)。- 論文發現：**Shear 和 Drift 在統計上是不相關的 (Uncorrelated)**。巨大的 Shear 可以發生在價格不動時（Liquidity Accumulation），而價格移動時 Shear 可能很小。- 這解釋了為什麼單純的 `OFI` (Order Flow Imbalance) 預測能力有限，因爲大部分 Imbalance 被吸收為幾何形變 (Shear)，而未轉化為價格位移。

• **Gamma Geometry**: - 假設市場沒有內在尺度 (Single-Scale Hypothesis)，流動性密度 $\tilde{\rho}(x)$ 必然服從 **Gamma 分佈**：
$$ q(x) \propto x^\gamma e^{-\lambda x} $$
    - $\gamma$: 控制近端曲率 (Local Curvature)。- $\lambda$: 控制遠端衰減 (Tail Decay)。- 實證數據 (AAPL, NVDA, TSLA) 顯示 Integrated Gamma 模型比 Power-law 或 Exponential 模型更準確。

---

### 🧠 HFT 與 Alpha 啟示 (Implications for HFT)

• **Beyond Imbalance**: - 我們目前的 Alpha 因子大量依賴 `Imbalance`。這篇論文警告我們：**Imbalance $\neq$ Price Pressure**。- 我們應該區分 **"Effective Shear"** (能推動價格的應力) 和 **"Plastic Shear"** (僅導致掛單變形但不會成交的應力)。- **Action**: 嘗試構建 `Shear_Elasticity` 因子：當 Shear 很大但價格不動時，說明市場處於 "Plastic Deformation" 階段（吸收流動性）；當 Shear 超過某個 Critical Point，才會發生 "Brittle Failure" (價格跳變)。

• **Shape Fitting**: - 不要在 LOB 數據中直接使用 10 檔掛單量做 Features。- 應該每個 Tick 擬合 Gamma 分佈參數 $(\gamma_t, \lambda_t)$，將整個 LOB 壓縮為這兩個參數。- $\Delta \gamma_t$ (曲率變化) 可能比單純的 Volume 變化更有預測力。

---

### 🚀 行動清單 (Action Items)

- [ ] **Feature Engineering**: 在 `LOB_Engine` 中實現 Gamma Distribution Fitting，計算每秒的 $(\gamma_{bid}, \lambda_{bid})$ 和 $(\gamma_{ask}, \lambda_{ask})$。
- [ ] **New Alpha**: 測試因子 `Shear_Stress = \gamma_{bid} - \gamma_{ask}` 對未來波動率的預測能力（而非對方向的預測能力）。
