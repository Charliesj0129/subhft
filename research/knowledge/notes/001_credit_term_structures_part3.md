# Defining, Estimating and Using Credit Term Structures Part 3: Consistent CDS-Bond Basis
ref: 001
Authors: Arthur M. Berd, Roy Mashal, Peili Wang (Lehman Brothers)
Published: 2004 (Presented), 2009 (ArXiv Upload)

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Defining, Estimating and Using Credit Term Structures Part 3: Consistent CDS-Bond Basis
• **作者**： Arthur M. Berd, Roy Mashal, Peili Wang (Lehman Brothers)
• **年份**： 2004 (Presented), 2009 (ArXiv Upload)
• **期刊/會議**： Lehman Brothers Fixed Income Research
• **引用格式**： Berd, A. M., Mashal, R., & Wang, P. (2004). Defining, Estimating and Using Credit Term Structures Part 3: Consistent CDS-Bond Basis.
• **關鍵詞**： #CDS-Bond_Basis #Credit_Arbitrage #Survival_Analysis #Hedging #Lehman_Brothers
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[FUTURES_ARB]], [[Credit_Modeling]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 傳統的信用債券估值指標（如 **Z-spread** 或 **Libor OAS**）存在根本性缺陷。它們通常基於「利差折現（spread-based discount）」邏輯，假設債券現金流是固定的（fixed），而忽略了信用債券的現金流其實是「承諾的（promised）」而非必然發生的。
- Z-spread 隱含假設回收率（Recovery Rate）為 0，這導致在不良債權（Distressed Debt）估值時嚴重高估風險，無法與 CDS（信用違約互換）市場進行公平比較。

• **研究目的**：

- 提出一套與 CDS 定價邏輯一致的債券估值框架（Survival-Based Valuation）。
- 定義 **"Bond-Implied CDS (BCDS)"**（債券隱含 CDS 利差），作為連接債券與 CDS 市場的橋樑。
- 開發一套 **Static Staggered Hedging Strategy**（靜態分層對沖策略），旨在利用 CDS 完全消除公司債的信用風險，從而分離出純粹的套利機會（Basis）。

• **理論框架**：

- **Reduced-Form Default Models** (Jarrow & Turnbull 1995, Duffie & Singleton 1999)：基於簡化形式的違約強度模型。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **研究範式**： 定量金融 / 衍生品定價 (Quantitative Finance / Derivatives Pricing)

• **核心模型 (BCDS Derivation)**：

- 作者不直接比較債券利差與 CDS 利差，而是先從債券價格反推「生存概率曲線（Survival Curve）」。
- 使用此生存曲線代入 CDS 定價公式（Eq [2]），計算出「如果該債券是 CDS，它的合理利差應該是多少」，即 **BCDS**。
- **公式核心**：
  $$ S\_{BCDS} = \frac{\sum P(0, t_i) Q(0, t_i) (1-R)}{\sum P(0, t_i) Q(0, t_i) \Delta t} $$
    其中 $Q(0, t)$ 是從債券價格擬合出的生存概率。

• **對沖策略 (The Hedge)**：

- 提出 **Staggered Forward CDS Strategy**：不能只用單一 CDS 對沖，因為債券價格會回歸面值（Pull-to-Par）。
- 必須針對未來的每一個時間段 $t_i$，根據當時的 Forward Price 建構不同名義本金（Notional）的 Forward CDS 對沖。
- 證明了「風險本身金（Risk-Free Equivalent Coupon, RFC）」與「對沖成本」之間的互補性（Complementarity）。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Z-spread 的誤導性**：在債券價格大幅折價（Distressed）時，Z-spread 會產生虛假的「利差倒掛（Inverted Curve）」，而 BCDS 能更準確反映真實信用風險結構。
2. **CDS-Bond Basis 的分解**：
   - **Curve Basis**：由流動性或供需造成的宏觀利差。
   - **Bond-Specific Basis**：特定債券與發行人曲線之間的偏差（由 OAS-to-Fit 衡量）。
3. **套利策略**：當市場 CDS 利差 < BCDS 時，表示 CDS 便宜，應買入債券並買入 CDS 保護（Negative Basis Trade），鎖定無風險利潤。

• **圖表摘要**：

- **Fig 1**：展示了 Georgia Pacific (GP) 公司的 Z-spread 與 BCDS 對比。Z-spread 顯示極端倒掛，而 BCDS 曲線更平滑且符合直覺。
- **Fig 2**：展示了對沖策略的現金流表，證明了 Staggered Hedge 可以將信用風險完全消除，僅留下無風險利率風險。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 這是非常經典的 **Basis Trading** 奠基之作。明確區分了 "Risk-Neutral Default Probability" 與傳統 Yield Spread 的數學關係。
- 提出的 **BCDS** 概念至今仍是許多對沖基金計算 Basis 的標準方法。

• **盲點/爭議**：

- **Lehman 的遺產**：這篇論文來自 Lehman Brothers (2004)，具有諷刺意味的是，文中假設的 "Risk-Free Rate" 和對手方風險在 2008 年後變得極為重要，而文中對 Counterparty Risk 著墨較少（當時視為次要）。
- **流動性假設**：Staggered Forward CDS 在實務中很難執行（流動性差），作者後來提出了 "Coarse-Grained"（粗粒度）近似法，這在實戰中更為可行。

---

### 📝 寫作語料庫 (Citable Material)

• **定義 (BCDS)**： "The bond-implied CDS spread term structure... is defined by substituting the survival probability term structure fitted from bond prices into the par CDS spread equation."

• **高質量論述**： "Z-spread overestimates the losses in case of default by a significant amount... because it assumes zero recovery implicitly." (很好的用來批評 Z-spread 的論點)

---

### 🚀 行動清單 (Action Items)

- [ ] **復現 BCDS 計算**：嘗試用我們的數據（如果有債券數據）計算簡單的 BCDS。
- [ ] **檢查 2026 論文中的 Basis 定義**：看現在的文獻（如 `2018 Optimal Dynamic Basis Trading`）是否引用或修正了這種定義。
- [ ] **Next Paper**: 閱讀 `2018 Optimal Dynamic Basis Trading` 以了解 Basis Trading 的現代演變。
