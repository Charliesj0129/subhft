# A unified theory of order flow, market impact, and volatility

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： A unified theory of order flow, market impact, and volatility
• **作者**： Johannes Muhle-Karbe, Youssef Ouazzani Chahdi, Mathieu Rosenbaum, Grégoire Szymanski
• **年份**： 2026 (February 2026; ArXiv Feb 2, 2026)
• **期刊/會議**： ArXiv:2601.23172 [q-fin.ST]
• **引用格式**： Muhle-Karbe, J., Chahdi, Y. O., Rosenbaum, M., & Szymanski, G. (2026). A unified theory of order flow, market impact, and volatility. arXiv preprint arXiv:2601.23172.
• **關鍵詞**： #Order_Flow #Market_Impact #Rough_Volatility #Hawkes_Processes #Market_Microstructure #Scaling_Limits
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Rough_Volatility_Models]], [[Propagator_Models]], [[Market_Impact_Laws]]

---

### 🎯 核心理論 (Core Theory)

• **The Grand Unification**: 作者提出了一個統一的微觀結構模型，通過這一個模型，可以同時解釋市場的三大特徵，且僅由**單一參數** $H_0$ 控制。

1. **Persistent Signed Order Flow**: 訂單流的長期記憶性（Hurst > 0.5）。
2. **Rough Traded Volume**: 成交量的粗糙性（Hurst < 0.5）。
3. **Power-law Market Impact**: 市場衝擊的冪律衰減（Square-root law）。

• **Two-Layer Hawkes Model**:

- **Layer 1: Core Flow ($F$)**: 由機構大單拆分（Metaorders）或趨勢策略產生的 "Autonomous" 流量。這部分具有長記憶性 $H_0$。
- **Layer 2: Reaction Flow ($N$)**: 由做市商、HFT 或流動性提供者產生的 "Endogenous" 流量。這部分是對 Core Flow 的 Martingale 響應。

---

### 🔢 關鍵公式與關係 (Key Formulas & Relations)

• **Master Parameter $H_0$**: Core Flow 的持久性指數。實證估計 $H_0 \approx 3/4 (0.75)$。
• **Scaling Relations**:

- **Signed Flow Roughness**: $H_{signed} \approx H_0 \approx 0.75$ (Persistent).
- **Unsigned Volume Roughness**: $H_{volume} = H_0 - 1/2 \approx 0.25$ (Rough).
- **Volatility Roughness**: $H_{vol} = 2H_0 - 3/2 \approx 0$ (Very Rough / Log-Normal).
- **Market Impact Exponent**: $\delta = 2 - 2H_0 \approx 0.5$ (Square Root Law).

• **Implication**: $H_0 = 3/4$ 是市場的 "Magic Number"。如果 $H_0=3/4$，則 Impact 準確遵循 Square Root Law，Volatility 是極度粗糙的 ($H \to 0$)。這完美解釋了為什麼 Rough Volatility 模型有效。

---

### 🧠 深度評析 & HFT 啟示 (Implications for HFT)

• **Propagator Model Parameterization**:

- 我們正在實作的 Propagator Model 通常需要擬合 Decay Kernel $\xi(t) \sim t^{-\gamma}$。
- 這篇論文告訴我們，這個 $\gamma$ 不是任意的，它由 Order Flow 的 $H_0$ 決定。
- **Action**: 我們應該先測量 Order Flow 的 $H_0$，然後直接推導出 Impact Kernel 的參數，而不是獨立擬合，這樣可以減少 Overfitting。

• **Volume Forecasting**:

- Unsigned Volume 是 rough 的 ($H \approx 0.25$)。這意味著 Volume 的預測不應該用簡單的 ARMA，而應該用能夠捕捉 Roughness 的模型（如 fractional ARIMA 或 T-KAN）。
- 短期的 Volume Spike 會迅速衰減，但其波動率本身具有長記憶性。

• **Market Making Strategy**:

- 區分 "Core" 和 "Reaction" 流量至關重要。
- **Core Flow**: 是有信息含量的，會造成永久衝擊。如果偵測到 $H_0$ 較高的流量，Spread 必須加寬。
- **Reaction Flow**: 是均值回歸的，是 "Noise"。這是我們作為 HFT 應該賺取的部分。

---

### 🚀 行動清單 (Action Items)

- [ ] **Data Analysis**: 在我們的數據上計算 Signed Order Flow 的 Hurst 指數。檢查是否接近 0.75。
- [ ] **Model Calibration**: 在校準 Propagator Model 時，嘗試固定 Impact Decay Exponent 為 $2 - 2H_{est}$，看是否能提高 Out-of-sample 表現。
