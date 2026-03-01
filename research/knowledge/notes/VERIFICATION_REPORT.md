# 研究論文假設驗證報告

## Verification Report for Research Paper Hypotheses

**驗證日期**: 2026-02-05
**驗證工具**: `research/tools/legacy/verify_paper_hypotheses_v2.py`

---

## 📊 驗證結果總覽

| Paper | 假設                     | 驗證結果    | 說明               |
| ----- | ------------------------ | ----------- | ------------------ |
| 026   | Signed Flow H ≈ 0.75     | ⚠️ 需要改進 | DFA 估計器需調校   |
| 026   | Scaling Relation         | ❌ 未支持   | 可能模擬方法問題   |
| 032   | Gamma Distribution       | ✅ **支持** | 90% 勝率           |
| 032   | Shear-Drift Decoupling   | ✅ **支持** | ρ = 0.02, p = 0.66 |
| 032   | Gamma Parameter Recovery | ✅ **支持** | r = 0.97           |

---

## 📝 詳細分析

### Paper 026: Unified Theory of Order Flow

**假設**:

- Signed Order Flow 具有持久性 (H ≈ 0.75)
- Unsigned Volume 具有粗糙性 (H ≈ 0.25)
- 兩者差 H_signed - H_unsigned ≈ 0.5

**驗證方法**:
使用 fractional Brownian motion + Hawkes process 模擬訂單流

**結論**:

- DFA 方法估計的 Hurst 指數偏高 (>1)，表明需要調整估計方法
- 差值約為 0.05 而非預期的 0.5
- **可能原因**:
  1. fBm 生成方法的 spectral method 可能有偏差
  2. DFA 對非平穩時序敏感
  3. 需要使用真實市場數據驗證

**建議行動**:

- [ ] 使用 `nolds` 或 `hurst` Python 庫重新驗證
- [ ] 獲取真實 LOB tick data 進行驗證

---

### Paper 032: Geometric Shear in Order Books

**假設 1: LOB 流動性服從 Gamma 分佈**

✅ **驗證通過**

- Gamma 模型在 90% 的 LOB 快照中優於 Exponential 模型
- 這支持論文的 "Single-Scale Hypothesis"

**假設 2: Shear 與 Drift 不相關**

✅ **驗證通過**

- Spearman 相關係數 ρ = 0.0195 (極低)
- p-value = 0.6642 (不顯著)
- 這確認了論文的核心發現：**Order Imbalance ≠ Price Pressure**

**假設 3: Gamma 參數可從數據恢復**

✅ **驗證通過**

- 真實 γ 與估計 γ 的相關係數 r = 0.9733
- 這意味著我們可以從 LOB 數據中提取 γ 作為有意義的 Alpha 因子

---

## 🚀 可行的 Alpha 因子

基於驗證結果，以下因子值得實作：

### 1. Gamma Shape Factor (Paper 032)

```python
# 每個 tick 計算 bid/ask 的 gamma 參數
gamma_bid, gamma_ask = fit_gamma_to_lob(levels, liquidity)
shear_stress = gamma_bid - gamma_ask

# 當 shear_stress 大但價格不動時 → 累積能量
# 當 shear_stress 突破閾值 → 可能爆發
```

### 2. Shear Energy Accumulator

```python
# 追蹤 shear 累積而未釋放的能量
shear_energy = cumsum(abs(shear_stress) * (1 - abs(price_return)))
# 當 energy > threshold → 高波動率前兆
```

### 3. LOB Curvature Differential

```python
# 近端曲率變化比 volume 變化更有預測力
curvature_delta = gamma_t - gamma_t_1
```

---

## ⚠️ 重要限制

1. **模擬 vs 真實數據**: 本驗證使用合成數據。真正的驗證需要交易所 Level II 數據
2. **Hurst 估計**: DFA 方法可能不適用於高度非平穩序列
3. **過擬合風險**: Gamma 擬合的 R² 可能因 Grid Search 而膨脹

---

## 📎 相關代碼

- `research/verify_paper_hypotheses.py` - 原始驗證 (已刪除，保留歷史記錄)
- `research/tools/legacy/verify_paper_hypotheses_v2.py` - 進階驗證 (使用 fBm + Hawkes)

---

## 下一步行動

1. [ ] 使用 `hftbacktest` 的真實 LOB 數據重新驗證
2. [ ] 實作 `GammaShapeFactor` 作為新的 Alpha 因子
3. [ ] 在回測中測試 Shear-Drift Decoupling 的交易含義
