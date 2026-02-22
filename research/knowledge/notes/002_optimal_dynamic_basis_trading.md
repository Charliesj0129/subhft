# Optimal Dynamic Basis Trading

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Optimal Dynamic Basis Trading
• **作者**： Bahman Angoshtari, Tim Leung (University of Washington)
• **年份**： 2018 (ArXiv 2019 v3)
• **期刊/會議**： ArXiv:1809.05961
• **引用格式**： Angoshtari, B., & Leung, T. (2019). Optimal Dynamic Basis Trading. arXiv preprint arXiv:1809.05961.
• **關鍵詞**： #Basis_Trading #Stochastic_Control #Brownian_Bridge #HJB_Equation #HARA_Utility
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[FUTURES_ARB]], [[Stochastic_Optimal_Control]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 傳統期貨定價或 Basis Trading 研究（如 Brennan & Schwartz 1988）通常假設 Basis 在到期日（Maturity, $T$）必然收斂為 0（Convergence）。
- 現實市場中（尤其是商品期貨），經常出現 **Non-Convergence** 現象（即到期時 $F_T \neq S_T$），這使得傳統無風險套利假設失效。
- 現有模型多含有套利機會（Arbitrage），而本文希望建立一個 **Arbitrage-Free** 的非收斂 Basis 模型。

• **研究目的**：

- 提出一個 **Stopped Scaled Brownian Bridge** 模型來描述 Basis，允許 Basis 在 $T$ 時不歸零，而是在虛擬時間 $T+\epsilon$ 歸零。
- 在 **HARA (Hyperbolic Absolute Risk Aversion)** 效用函數下，推導最優動態交易策略（Optimal Dynamic Trading Strategy）。
- 解決 HJB 方程的 **Well-posedness** 問題，找出效用爆炸（Infinite Expected Utility）的臨界條件。

• **理論框架**：

- **Stochastic Control Theory** (Hamilton-Jacobi-Bellman Equation).
- **Brownian Bridge** 隨機過程變體。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **研究範式**： 隨機最優控制 (Stochastic Optimal Control)

• **核心模型 (The Model)**：

- **價格動態**：
  $$ \frac{dS*t}{S_t} = \mu_1 dt + dW*{t,1} $$
    $$ \frac{dF*t}{F_t} = \left( \mu_2 + \frac{\kappa Z_t}{T-t+\epsilon} \right) dt + \rho dW*{t,1} + \sqrt{1-\rho^2} dW\_{t,2} $$
- **Stochastic Basis ($Z_t$)**：定義為 $Z_t = \log(S_t / F_t)$。
- **Basis SDE** (Lemma 2.2)：
  $$ dZ*t = \left( \mu_1 - \mu_2 - \frac{\kappa Z_t}{T-t+\epsilon} \right) dt + (1-\rho)dW*{t,1} - \sqrt{1-\rho^2}dW\_{t,2} $$
- **關鍵參數 $\epsilon$**：如果你設 $\epsilon=0$，則 $Z_T=0$（強制收斂）。本文設 $\epsilon > 0$，使得 Basis 傾向於收斂但在 $T$ 時仍有隨機性，這消除了無風險套利機會（Proposition 2.5）。

• **優化問題**：

- 目標：最大化終端財富的期望效用 $J(t,x,z) = \sup_{\pi} E[ U(X_T) ]$。
- 效用函數 $U(x)$：HARA 類（包含冪函數效用 Power Utility, 指數效用 Exponential Utility）。

• **求解方法**：

- 推導 HJB 方程並猜測解的形式：
  $$ v(t,x,z) = U(x) \exp\left( f(t) + g(t)z + \frac{1}{2}h(t)z^2 \right) $$
- 將 PDE 簡化為關於 $h(t)$ 的 **Riccati Equation**（常微分方程）。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **最優策略形式**：得到的策略 $\pi^*$ 是「線性反饋控制（Linear Feedback Control）」，依賴於當前的 Basis 水平 $Z_t$。
   $$ \pi^\*(t,x,z) = \text{RiskTolerance}(x) \times (\text{Drift Terms} + z \times \text{MeanReversion}) $$
2. **Nirvana Strategies (效用爆炸)**：
   - 若風險容忍度 $\gamma$ 超過臨界值 $\bar{\gamma}$，且投資期限 $T$ 超過「逃逸時間（Escape Time）」$T^*(\gamma)$，則期望效用會趨向無窮大。
   - 這意味著在某些參數下，模型允許「過度激進」的策略，這在數學上被稱為 ill-posed，但在其邊界內是可解的。
3. ** $\epsilon$ 的重要性**： $\epsilon$ 越小，Mean Reversion 力道在接近到期時越強，持倉量會越大。

• **圖表摘要**：

- **Fig 1**：展示了 $S_t, F_t$ 和 $Z_t$ 的模擬路徑。可以看到 $Z_t$ 在 $T$ 時並未完全收斂到 0，而是分佈在 0 附近的一個區間（由 $\epsilon$ 控制）。
- **Fig 3**：展示了 Riccati 方程解 $h(t)$ 的形態，這直接決定了對 Basis 的敏感度（Position Size）。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- **無套利模型**：通過引入 $\epsilon$，優雅地解決了布朗橋模型在 $T$ 時的奇異性（Singularity）和套利問題。這是一個非常紮實的數學處理。
- **解析解**：提供了 HARA 效用下的閉式解，這對於需要快速計算（HFT/Algo Trading）的實踐者非常有價值，不需要跑蒙特卡洛。

• **邏輯一致性**：

- 數學推導非常嚴謹（附錄證明了 HJB 的驗證定理和 Riccati 方程的解）。
- 對 "Infinite Expected Utility" 的討論顯示了作者對模型邊界的深刻理解。

• **盲點分析**：

- **交易成本**：如同大多數理論控制論文，模型假設無交易成本。但在 Basis Trading 中，頻繁調整倉位（尤其是接近 $T$ 時 $\kappa/(T-t+\epsilon)$ 變大）會產生巨大滑點。
- **跳躍風險**：模型是純擴散（Diffusion），沒有考慮價格跳躍（Jumps），而 Basis 往往在極端市場情境下會有跳躍。

---

### 📝 寫作語料庫 (Citable Material)

• **定義 (Stochastic Basis)**: "$Z_t := \log(S_t/F_t)$ modeled by a scaled Brownian bridge stopped before its convergence."
• **高質量論述**: "In reality, basis trading is far from a riskless arbitrage... unexpected changes in cost of carry or market frictions can turn seemingly certain arbitrage into disastrous trades."

---

### 🚀 行動清單 (Action Items)

- [ ] **模型實作**：將此論文的 $\pi^*$ 公式（Eq 3.7）寫成 Python 函數，作為我們的一個策略候選。特別是那個 `feedback form`。
- [ ] **參數校準**：我們需要估計 $\mu_1, \mu_2, \kappa, \epsilon, \rho$。這可以用歷史數據的 MLE（最大似然估計）來做。
- [ ] **與 Alpha-H0 結合**：可以把這個 Basis Reversion 作為一個 Alpha 信號，疊加在 Order Flow Imbalance 上。
