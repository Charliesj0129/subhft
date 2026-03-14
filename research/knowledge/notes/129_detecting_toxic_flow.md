# Detecting Toxic Flow
ref: 129
arxiv: https://arxiv.org/abs/2312.05827
Authors: Alvaro Cartea, Gerardo Duran-Martin, Leandro Sanchez-Betancourt
Published: 2023

## 深度學術論文筆記 (Deep Academic Note)

### 基礎元數據 (Metadata)
- **標題**： Detecting Toxic Flow
- **作者**： Alvaro Cartea, Gerardo Duran-Martin, Leandro Sanchez-Betancourt
- **年份**： 2023
- **期刊/會議**： ArXiv:2312.05827
- **關鍵詞**： #toxic_flow #adverse_selection #lob_features #bayesian #microstructure
- **閱讀狀態**： 已完成
- **關聯項目**： [[Alpha_Factor_Engineering]], [[Toxic_Flow]]

---

### 研究背景與目標 (Context & Objectives)
- **Research Gap**:
Existing toxicity detection relies on simple metrics such as VPIN (Volume-Synchronized Probability of Informed Trading). These approaches lack real-time predictive capability and fail to exploit the rich feature set available from limit order book data.

- **研究目的**:
Develop a real-time toxicity prediction framework (PULSE) that uses Bayesian online learning for neural networks, leveraging 168 LOB features (8 statistics x 3 clocks x 7 intervals) plus 15 client-specific features to predict adverse selection before it materializes.

---

### 研究方法論 (Methodology)
- **PULSE Framework**: Bayesian online learning applied to neural networks for sequential toxicity prediction.
- **Feature Engineering**: 168 LOB features constructed from 8 statistics across 3 time clocks (trade, volume, calendar) and 7 lookback intervals, plus 15 client-level features.
- **Key Innovation**: Variance decomposition by side — positive/negative QI squared captures burst patterns in order flow. The spread x imbalance interaction term is found to be highly predictive of toxic episodes.

---

### 結果與討論 (Results & Discussion)
- Volatility, volume imbalance, and bid-ask spread are the strongest predictors of toxic flow.
- The model achieves sub-millisecond prediction latency (<1ms per prediction), making it viable for real-time HFT applications.
- Bayesian uncertainty quantification allows the model to express confidence in its toxicity predictions, enabling adaptive risk management.

---

### 深度評析 (Synthesis & Critique)
- **核心貢獻**: Provides a principled, real-time framework for toxicity detection that goes beyond VPIN. The feature engineering approach (multi-clock, multi-interval LOB stats) is directly applicable to our platform.
- **對 HFT 的啟示**: Variance decomposition by side (positive/negative QI squared) captures burst patterns. The spread x imbalance interaction is predictive — this informs our toxicity_acceleration and adverse_flow_asymmetry alpha designs.

---

### 行動清單 (Action Items)
- [ ] Implement `toxicity_acceleration` alpha based on variance decomposition of QI by side
- [ ] Implement `adverse_flow_asymmetry` alpha using buy/sell flow asymmetry detection
- [ ] Evaluate spread x imbalance interaction as additional feature in FeatureEngine
