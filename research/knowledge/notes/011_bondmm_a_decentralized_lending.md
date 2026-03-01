# BondMM-A: Decentralized Fixed-Income Lending AMM Supporting Arbitrary Maturities
ref: 011
arxiv: https://arxiv.org/abs/2512.16080
Authors: Tianyi Ma (Shanghai Jiao Tong University)
Published: 2025 (December 2025; ArXiv Dec 18, 2025)

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Design of a Decentralized Fixed-Income Lending Automated Market Maker Supporting Arbitrary Maturities
• **作者**： Tianyi Ma (Shanghai Jiao Tong University)
• **年份**： 2025 (December 2025; ArXiv Dec 18, 2025)
• **期刊/會議**： ArXiv:2512.16080 [cs.CR]
• **引用格式**： Ma, T. (2025). Design of a Decentralized Fixed-Income Lending Automated Market Maker Supporting Arbitrary Maturities. arXiv preprint arXiv:2512.16080.
• **關鍵詞**： #DeFi #AMM #Fixed_Income #Yield_Curve #BondMM #Smart_Contract
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[DeFi_Arbitrage]], [[Yield_Protocol]], [[AMM_Design]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 現有的 DeFi 固定收益協議（如 Yield Protocol, Notional）通常只支持單一到期日（Single Maturity）。
- 這導致了流動性割裂（Fragmentation）：每個到期日都需要一個獨立的流動性池。
- BondMM (Tran et al. 2024) 引入了基於現值（Present Value）的不變量，但仍限於單一期限。

• **研究目的**：

- 提出 **BondMM-A**：一個支持任意到期日（Arbitrary Maturities）的固定收益 AMM。
- 允許用戶在同一個池子中借貸任意期限的資金（從 1 天到 10 年）。
- 對 LP 而言，只需維護一個統一的流動性池，極大提高了資本效率。

• **理論框架**：

- **Present Value Tokenization**: 不再 Tokenize 債券面值（Face Value），而是跟蹤債券的 Present Value ($X$)。
- **Invariant**: 基於 BondMM 的不變量 $y^\alpha (\frac{X}{y} + 1) = C$ 的擴展。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **BondMM-A Pricing Logic**：

- **State**: AMM 狀態由 $(x, y)$ 決定，其中 $y$ 是現金池，$x$ 是債券池（折算為 Maturity $T$ 的當量）。
- **Rate Function**: 瞬時利率 $r$ 由供需比 $\psi = X/y$ 決定：
  $$r = \kappa \ln(X/y) + r^*$$
  其中 $X = x p = x e^{-rt}$ 是債券現值。
- **Pricing**: 當用戶交易（借/貸）時，AMM 根據上述公式計算邊際價格 $p = e^{-rt}$。
- **Multi-Maturity**: 通過動態調整 $r^*$（Anchor Rate）作為期限 $t$ 的函數（$r^*(t)$），AMM 可以模擬出一條 Yield Curve。

• **Arbitrage Mechanism**：

- 論文假設存在 "Active Traders"（Speculators），如果 BondMM-A 的利率高於市場利率（Market Rate），他們就會 Lending（存錢獲利）；反之則 Borrowing。
- 這種套利行為會將 BondMM-A 的利率曲線推向市場均衡。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Curve Tracking**: 實驗顯示 BondMM-A 的利率曲線能緊密跟隨市場利率（由 CIR 模型生成），誤差極小（$10^{-5}$ 量級）。
2. **Stability**: LP 的 Net Equity 保持穩定，未出現顯著虧損（Impermanent Loss 被利息收入抵消）。
3. **Efficiency**: 相比於 Yield Protocol 需要為每個期限建立 Pool，BondMM-A 的單池設計極大降低了 Gas 成本和流動性門檻。

• **圖表摘要**：

- **Fig 1**: BondMM-A Rate vs Market Rate 基本重合。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 解決了 DeFi 固定收益市場的 "Term Structure" 問題。這有點像 Uniswap v3 解決了價格區間問題，BondMM-A 解決了時間區間問題。
- 對於 HFT/Arbitrageur 來說，這是一個潛在的金礦。這種複雜數學模型（對數定價）通常會在極端市場條件下（高波動、流動性抽乾）出現定價錯誤。

• **對 HFT 的啟示**：

- **DeFi Rates Integration**: 我們應該從鏈上獲取 BondMM-A（如果上線）的 Yield Curve 數據，作為 Funding Rate 的預測因子。
- **Cross-Venue Arb**: 如果 BondMM-A 的 $r_{1yr}$ 顯著高於 Binance Futures Funding Rate (年化)，則存在 "Long Spot + Short Perp + Lend on BondMM-A" 的無風險套利機會。

---

### 📝 寫作語料庫 (Citable Material)

• **定義**: "BondMM-A supports arbitrary maturities... LPs provide liquidity to a unified pool, eliminating capital fragmentation."

---

### 🚀 行動清單 (Action Items)

- [ ] **監控合約地址**: 關注 Github `HarryTMa/BondMMA`，一旦主網部署，立即集成到我們的 DeFi 監控列表。
- [ ] **Yield Curve Arb Model**: 寫一個簡單的腳本，實時計算 `BondMM_Rate - Perp_Funding_Rate` 的 Spread。
