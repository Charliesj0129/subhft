# The Fair Basis: Funding and capital in the reduced form framework

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： The Fair Basis: Funding and capital in the reduced form framework
• **作者**： Wujiang Lou
• **年份**： 2017 (Updated 2019/2020)
• **期刊/會議**： ArXiv / Working Paper
• **引用格式**： Lou, W. (2017). The Fair Basis: Funding and capital in the reduced form framework. Available at SSRN/arXiv.
• **關鍵詞**： #CDS-Bond_Basis #Negative_Basis #XVA #FVA #KVA #Capital_Charges
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[FUTURES_ARB]], [[Credit_Valuation_Adjustment]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 傳統的 Reduced-Form 模型（如 Duffie & Singleton 1999）假設無摩擦市場，認為 CDS-Bond Basis（CDS 利差 - 債券利差）應該為零或僅由流動性解釋。
- 2008 金融危機時，Basis 出現極端負值（Negative Basis），導致許多套利者（Arbitrageurs）鉅額虧損。
- 傳統模型忽略了 **Funding Cost (資金成本)** 和 **Capital Charges (資本佔用成本)** 對交易定價的影響。

• **研究目的**：

- 建立一個包含資金成本（Funding）和經濟資本（Economic Capital）的債券定價模型。
- 解釋為何在考慮各類 XVA（FVA, KVA）後，**"Fair Basis"** 實際上應該是負的，而不是零。
- 為負基差交易（Negative Basis Trade）提供一個更合理的相對價值度量標準。

• **理論框架**：

- **Reduced Form Credit Model**: 基於強度（Intensity-based）的違約模型。
- **Capital & Funding**: 引入 FVA（Funding Valuation Adjustment）和 KVA（Capital Valuation Adjustment）。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **研究範式**： 定量金融 / 衍生品定價 (Quantitative Finance / XVA)

• **核心模型**：

1. **Hedging Economy (對沖經濟體)**：
   - 建構一個包含銀行賬戶、回購融資（Repo）、債務發行（Debt）、經濟資本儲備（EC Reserve）的自融資組合。
   - 考慮債券購買資金來自 Repo（有 Haircut $h$）和無擔保融資（Unsecured Funding）。
2. **Wealth Equation (財富方程)**：
   - 推導包含違約跳躍風險（Jump-to-Default Risk）和融資成本的偏微分方程（PDE）。
   - PDE (Eq 16) 中引入了顯式的融資利率 $r_p$（Repo Rate）和資本回報率 $r_k$。
3. **Fair Basis Formula**：
   $$ (r*c - S - z)*{fair} = (\bar{r}\_p - z) + \bar{r}\_k N_c $$
   - 左邊是 Basis。
   - 右邊第一項 $(\bar{r}_p - z)$ 是有效融資成本（Effective Funding Cost）。
   - 右邊第二項 $\bar{r}_k N_c$ 是資本成本（Cost of Economic Capital）。

• **經濟資本 (Economic Capital, EC)**：

- 即使 Delta Neutral，信用對沖仍存在 **Residual Jump Risk**（因為 CDS 與債券違約結算機制不同及相關性問題）。
- 對此殘餘風險收取資本費用（Capital Charge），即 KVA。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Basis 不應為零**：Fair Basis = Funding Cost + Capital Cost。
2. **負基差的合理性**：因為融資成本通常高於無風險利率，且持有風險資產需要佔用資本（KVA），所以 CDS 利差（保護成本）通常會低於債券利差（收益），導致基差為負。
3. **模型驗證**：數值模擬顯示，對於 IG（投資級）債券，Fair Basis 大約在 -60 到 -120 bps 之間，這這解釋了為何市場上長期存在負基差。

• **圖表摘要**：

- **Fig 1**: 2005-2018 年的歷史 Basis 走勢，顯示 2008 年危機期間 Basis 極度拓寬（負得很深）。
- **Fig 3**: 展示了不同融資利率和資本要求下，Fair Basis 的變化。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 將 "Arbitrage" 重新定義為 "Trade with Costs"。明確指出 Negative Basis Trade 並非無風險套利，而是賺取流動性溢價和資本成本的補償。
- 公式 (23) 提供了一個非常直觀的 Basis 估值基準。

• **盲點/爭議**：

- **Haircut 的影響**：文中對 Haircut 的動態變化討論較少，實際上危機時 Haircut 飆升是導致 Basis 崩潰的主因（Margin Call 導致被迫平倉）。

---

### 📝 寫作語料庫 (Citable Material)

• **定義 (Fair Basis)**: "The fair basis consists of the effective funding cost and cost of economic capital."
• **高質量論述**: "To classic reduced form model theorists, the existence of the basis is an abnormality... Such a view fails to explain large basis trading losses incurred during the financial crisis."

---

### 🚀 行動清單 (Action Items)

- [ ] **檢查我們的融資成本**：如果我們要交易 Basis，我們的資金成本（Cost of Leverage）是多少？這直接決定了我們的 Fair Basis。
- [ ] **計算 KVA**：如果我們使用自有資金（Prop Trading），KVA 可以視為 Opportunity Cost；如果是外部資金，則需考慮資金方的回報要求。
