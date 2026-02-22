# Asymptotic and Finite-Sample Distributions of Empirical Relative Entropy for Change-Point Detection

## 📄 深度學術論文筆記 (Deep Academic Note)

### 📌 基礎元數據 (Metadata)

• **標題**： Asymptotic and finite-sample distributions of one- and two-sample empirical relative entropy, with application to change-point detection
• **作者**： Matthieu Garcin & Louis Perot
• **年份**： 2025 (December 19, 2025; ArXiv Dec 2025)
• **期刊/會議**： ArXiv:2512.16411 [stat.ME]
• **引用格式**： Garcin, M., & Perot, L. (2025). Asymptotic and finite-sample distributions of one- and two-sample empirical relative entropy. arXiv preprint arXiv:2512.16411.
• **關鍵詞**： #Change_Point_Detection #Relative_Entropy #Kullback_Leibler #Berry_Esseen #Regime_Detection
• **閱讀狀態**： 🟢 已完成
• **關聯項目**： [[Regime_Switching_Models]], [[Statistical_Arbitrage]], [[Market_Microstructure]]

---

### 🎯 研究背景與目標 (Context & Objectives)

• **Research Gap (研究缺口)**：

- 現有的 Change-Point Detection (CPD) 方法多基於矩（Moment-based），如 CUSUM 檢測均值或方差的跳變。
- 然而，金融市場的結構性變化（Structural Break）往往體現在分佈形狀的改變，而不僅僅是均值漂移（例如尾部風險變厚，但均值不變）。
- 基於分佈的 CPD 常用 Relative Entropy (KL Divergence)，但其小樣本分佈性質未知，導致難以設定確切的統計顯著性閾值。

• **研究目的**：

- 推導 Empirical Relative Entropy 的漸近分佈（Asymptotic Distribution）和小樣本下的有限樣本界（Finite-Sample Bounds）。
- 提出一種基於 KL 散度的穩健 Change-Point Detection 檢驗方法。
- 將其應用於波動率序列的結構性斷裂檢測。

• **理論框架**：

- **Kullback-Leibler Divergence (KL)**: $D_{KL}(\hat{p}_n \| \hat{q}_m)$。
- **Berry-Esseen Theorem**: 用於推導非線性統計量的收斂速度和誤差界。
- **Concentration Inequalities**: Sanov, Mardia, Agrawal inequality。

---

### 🛠 研究方法論 (Methodology - 深度拆解)

• **核心定理**：

- **Theorem 1 (One-Sample)**: $2n D_{KL}(\hat{p}_n \| p) \xrightarrow{d} \chi^2_{k-1}$。
- **Theorem 2 (Two-Sample)**: $2 \frac{nm}{n+m} D_{KL}(\hat{p}_n \| \hat{q}_m) \xrightarrow{d} \chi^2_{k-1}$。
- 這意味著在沒有變點（Null Hypothesis）的情況下，兩個子樣本的 KL 散度應服從卡方分佈。如果算出的統計量顯著高於卡方分佈的閾值，則拒絕原假設，認為存在變點。

• **CPD 算法**：

- 給定時間序列 $X_1, ..., X_{2n}$。
- 假設變點在中間（Offline detection），將其分為兩半：$X_1...X_n$ 和 $X_{n+1}...X_{2n}$。
- 計算這兩半的 Empirical Discretized Probability Distributions $\hat{p}, \hat{q}$。
- 計算 Test Statistic: $T = 2 \frac{n^2}{2n} D_{KL}(\hat{p} \| \hat{q})$。
- 與 $\chi^2_{k-1}$ 的 quantile 比較。

• **模擬實驗**：

- 對比了 T-test (Mean), F-test (Variance), AIC (Model Selection) 和 KL-based 方法。
- 結果顯示 KL 方法在非均值漂移（如分佈形狀變化）的檢測上具有更高的 Power。

---

### 📊 結果與討論 (Results & Discussion)

• **主要發現 (Primary Results)**：

1. **Asymptotic Utility**: 推導出的漸近分佈非常精確，即使在 $n=50$ 的小樣本下，也能很好地近似真實分佈。
2. **Robustness**: 對於金融波動率序列（如 2008 年危機、2020 年新冠），KL 方法能準確捕捉到市場體制的切換點，且比單純的 Volatility Break Test 提供更多信息（因为它捕捉了整個分佈的變化）。

• **圖表摘要**：

- **Fig 1**: 展示了 Empirical KL 的 CDF 與理論 $\chi^2$ 分佈的高度重合。
- **Table**: 在各種信噪比下，KL-based 檢驗的 Power consistently 高於基於矩的方法。

---

### 🧠 深度評析 (Synthesis & Critique)

• **核心貢獻**：

- 為 "Regime Detection" 提供了一個堅實的統計學基礎。
- 在 HFT 中，我們經常談論 "Regime Shift"，但往往依賴黑箱模型（HMM）或簡單的閾值。這篇論文告訴我們可以用一個簡單的 $\chi^2$ 檢驗來嚴格地判斷當前市場是否發生了結構性變化。

• **對 HFT 的啟示**：

- **Feature Engineering**: 我們應該構建一個 "Regime Signal"：計算過去 5 分鐘 vs 過去 30 分鐘的 Order Book 分佈的 KL 散度。如果該值突然飆升並超過閾值，說明市場進入了新狀態（可能是流動性崩潰，或大單入場）。
- **Alpha-R1 Input**: 這個信號是輸入給 Alpha-R1 的絕佳 $S_t$ 特徵。

---

### 📝 寫作語料庫 (Citable Material)

• **方法論描述**: "The offline approach makes it possible to compare probabilities... instead of only moments."
• **優勢**: "Relative entropy... is the statistic leading to the uniformly highest power... under the assumptions of Neyman-Pearson lemma."

---

### 🚀 行動清單 (Action Items)

- [ ] **實現 KL-Detector**: 編寫一個 Python 函數 `calc_regime_shift_score(window_recent, window_ref)`，計算兩個窗口 return 分佈的 KL 散度。
- [ ] **集成到 Market Monitor**: 在我們的實盤監控中加入這個指標，當 Score > Chi2_Threshold 時發出 "Regime Shift Alert"。
