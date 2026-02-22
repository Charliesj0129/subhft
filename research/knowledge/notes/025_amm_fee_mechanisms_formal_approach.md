# A Formal Approach to AMM Fee Mechanisms with Lean 4

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： A Formal Approach to AMM Fee Mechanisms with Lean 4
• **作者**： Marco Dessalvi, Massimo Bartoletti, Alberto Lluch-Lafuente
• **年份**： 2026 (January 2026; ArXiv Jan 24, 2026)
• **期刊/會議**： ArXiv:2602.00101 [q-fin.MF]
• **引用格式**： Dessalvi, M., Bartoletti, M., & Lluch-Lafuente, A. (2026). A Formal Approach to AMM Fee Mechanisms with Lean 4. arXiv preprint arXiv:2602.00101.
• **關鍵詞**： #DeFi #AMM #Formal_Verification #Lean4 #Arbitrage #Fee_Mechanisms #Uniswap_v2
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[AMM_Math]], [[Arbitrage_Optimization]], [[Smart_Contract_Verification]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Context**: 現有的 AMM 理論模型（如 CPMM）通常為了數學簡化而忽略交易手續費（$\phi = 1$）。但在現實中，手續費（$\phi < 1$）顯著改變了用戶策略和市場動態。
• **Problem**: 手續費的存在破壞了許多無手續費模型中的優雅屬性（如可加性和可逆性）。
• **Goal**: 使用形式化證明工具 **Lean 4**，精確建模帶手續費的 AMM 機制，並推導出新的經濟屬性和最優套利策略。

---

### 🛠 關鍵發現與定理 (Key Findings & Theorems)

• **1. Breakdown of Additivity (可加性失效)**:

- 在無手續費時，拆單（Splitting）不影響總產出。
- 在有手續費時（$\phi < 1$），**一次性大單的收益嚴格大於拆成兩筆小單**。
- Theorem 8 證明了這一點。這對於我們的 Execution Algo 有重要啟示：在 DEX 上，為了最大化收益，應該盡量減少交易次數，而不是像 Order Book 交易所那樣為了隱藏蹤跡而拆單（除非考慮 Slippage Price Impact，但在這個純數學模型中，只考慮 Fee Impact，大單更優）。

• **2. Arbitrage: Equilibrium vs. Optmality (套利均衡 vs 最優性)**:

- **$x_0$ (Equilibrium Value)**: 使 Pool Price 與外部市場價格一致的交易量。
- **$x_{max}$ (Optimal Arbitrage Value)**: 使套利者利潤最大化的交易量。
- **Findings**: 帶手續費時，**$x_{max} > x_0$**。也就是說，為了最大化利潤，套利者應該將價格推得**略微超過**外部市場價格。這是因為手續費創造了一個 "Fee Wedge"。
- Theorem 13 提供了 $x_{max}$ 的閉式解（Closed-form solution）。

---

### 🧠 深度評析 & HFT 啟示 (Implications for HFT)

• **DEX Arbitrage Strategy**:

- 如果我們的套利 Bot 僅僅將價格推到平價（$x_0$），我們就**把錢留在桌子上了**。我們必須計算 $x_{max}$。
- 公式：
  $$x_{max} = x_0 + \frac{-\sqrt{P(\tau_0)r_0} -\sqrt{P(\tau_0)\phi}x_0 + \sqrt{P(\tau_1)\phi r_0 r_1}}{\sqrt{P(\tau_0)\phi}}$$
- 這是一個可以直接寫入我們 Rust Engine 的公式。

• **Execution Logic**:

- 在 CEX 上，我們習慣拆單（TWAP/VWAP）來減少 Impact。但在 AMM 上，由於固定比例費用的存在，拆單會導致直接的數額損失（Fee Drag）。必須在 Price Impact 和 Fee Drag 之間找到新的平衡。對於流動性足夠的池子，**Lump Sum** 可能優於 **Splitting**。

• **Verification**:

- 這篇論文證明了 Lean 4 在金融工程中的潛力。對於我們未來的核心合約或關鍵算法，可以考慮引入形式化證明，保證沒有溢出或邊界條件錯誤。

---

### 🚀 行動清單 (Action Items)

- [ ] **Implementation**: 在 `hft_platform/strategies/dex_arb.py` 中更新套利計算邏輯，將目標交易量從 $x_0$ 改為 $x_{max}$。
- [ ] **Backtest**: 使用歷史數據回測 $x_0$ 策略 vs $x_{max}$ 策略的累計利潤差異。預期 $x_{max}$ 策略的 PnL 會高出 1-3%（取決於費率和波動率）。
