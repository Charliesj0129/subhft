# Stochastic Volatility Modelling with LSTM Networks: A Hybrid Approach

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Stochastic Volatility Modelling with LSTM Networks: A Hybrid Approach for S&P 500 Index Volatility Forecasting
• **作者**： Anna Perekhodko, Robert Ślepaczuk (University of Warsaw)
• **年份**： 2025 (December 2025; ArXiv Dec 13, 2025)
• **期刊/會議**： ArXiv:2512.12250 [q-fin.TR]
• **引用格式**： Perekhodko, A., & Ślepaczuk, R. (2025). Stochastic Volatility Modelling with LSTM Networks: A Hybrid Approach. arXiv preprint arXiv:2512.12250.
• **關鍵詞**： #Stochastic_Volatility #LSTM #Hybrid_Models #Volatility_Forecasting #S&P500
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Volatility_Modeling]], [[Deep_Learning_Forecasting]], [[Risk_Management]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 傳統計量經濟學模型（SV, GARCH）擅長捕捉統計特性（如 Volatility Clustering, Mean Reversion），但在處理非線性模式時能力有限。
- 深度學習模型（LSTM）擅長模式識別，但缺乏統計結構的約束，對噪聲敏感。
- 缺乏將 SV 模型的潛在變量（Latent Volatility）作為 Feature 輸入給 LSTM 的混合研究。

• **研究目的**：

- 構建 **Hybrid SV-LSTM Model**：將 SV 模型的預測結果作為額外特徵輸入到 LSTM 中。
- 驗證假設 H1-H3：混合模型優於單一的 SV 或 LSTM 模型。

• **Data**:

- S&P 500 Index Daily Close (1998-2024).
- Rolling Window: 11 years train, 3 years val, 1 year test (Walk-Forward).

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **SV Component**:

- Standard SV Model (Taylor, 1982) estimated via MCMC (Bayesian).
- Output: Predicts latent log-volatility $h_{t+1}$.
- 使用 `stochvol` R package 進行估計。

• **LSTM Component**:

- Inputs: Log Returns, 21-day Historical Volatility, **SV Forecast ($h_{t+1}$)**.
- Architecture: 3-layer LSTM with Dropout/Dense layers.
- Hyperparameter Tuning: Random Search via Keras Tuner.

• **Hybrid Logic**:

- SV 模型提供一個 "Denoised" 的基礎波動率信號（Base Signal）。
- LSTM 模型在此基礎上學習非線性的殘差調整（Residual Adjustment）。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

- **Performance**: Hybrid SV-LSTM (MAPE 4.75%) > LSTM (5.29%) > SV (18.12%)。
- **Error Reduction**: 相比純 LSTM，混合模型降低了約 10% 的相對誤差。相比純 SV，降低了 73% 的誤差（說明純 SV 對短期噪聲過於敏感）。
- **Robustness**: 混合模型在市場極端波動期間（如 2020 COVID Crash）表現更穩定，能夠更快適應波動率的劇烈變化。

• **Statistical Test**:

- Diebold-Mariano Test 確認 Hybrid 模型顯著優於 Benchmark 模型 (p < 0.05)。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 證明了 **"Statistical Feature Engineering"** 的有效性。與其讓 DL 模型從 Raw Returns 中學習所有東西，不如先用傳統模型（SV）提取核心特徵（Latent Vol），再餵給 DL。這是一種有效的 "Inductive Bias" 注入方式。

• **對 HFT 的啟示**：

- **Volatility Feature**: 在我們的 HFT 模型中，不要只放 Raw OHLCV。應該加入 GARCH 或 SV 模型的預測值作為 Feature。
- **Latency**: 雖然 MCMC 估計 SV 很慢（論文中用了 24小時調參），但在推理階段（Inference），如果是已經擬合好的參數，計算是很快的。或者可以用 GARCH 替代 SV 以換取速度（GARCH 是解析解/數值優化，比 MCMC 快）。
- **Regime Detection**: SV 模型的 Latent State $h_t$ 本身就是一個極好的 Regime Indicator。

---

### 🚀 行動清單 (Action Items)

- [ ] **Feature Engineering**: 在 `research/alphas/` 的對應 `impl.py` 中增加 `GARCH_Vol_Forecast` 作為新的 Alpha 因子（SV 計算太慢，GARCH 更適合高頻實時）。
