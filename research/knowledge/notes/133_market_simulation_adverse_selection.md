# Market Simulation under Adverse Selection
ref: 133
arxiv: https://arxiv.org/abs/2409.12721
Authors: (not specified)
Published: 2024

## 深度學術論文筆記 (Deep Academic Note)

### 基礎元數據 (Metadata)
- **標題**： Market Simulation under Adverse Selection
- **作者**： (not specified)
- **年份**： 2024
- **期刊/會議**： ArXiv:2409.12721
- **關鍵詞**： #adverse_selection #fill_probability #simulation #market_making #microstructure
- **閱讀狀態**： 已完成
- **關聯項目**： [[Alpha_Factor_Engineering]], [[Toxic_Flow]]

---

### 研究背景與目標 (Context & Objectives)
- **Research Gap**:
Independent simulation of price dynamics and order fills inflates strategy performance. Most backtesting frameworks model fills independently of price movements, ignoring the correlation between fill probability and subsequent adverse price moves (adverse selection).

- **研究目的**:
Develop a joint fill probability and adverse fill model calibrated on CME futures data (ES, NQ, CL, ZN). Demonstrate that ignoring adverse selection in simulation leads to significantly overstated performance metrics.

---

### 研究方法論 (Methodology)
- **Joint Modeling**: Fill probability is modeled jointly with subsequent price movement, capturing the adverse selection effect where fills on one side are correlated with price moves against the filled position.
- **Empirical Calibration**: Model parameters are calibrated on CME futures data across four liquid contracts (ES, NQ, CL, ZN).
- **Performance Comparison**: Strategy performance is compared between independent fill simulation and adverse-selection-aware simulation.

---

### 結果與討論 (Results & Discussion)
- Fill probabilities and adverse fills significantly affect simulated performance — ignoring them leads to overstated Sharpe ratios and understated drawdowns.
- Adverse fills cluster on one side of the book, creating a measurable asymmetry signal.
- The joint model is essential for realistic backtesting of market-making and HFT strategies.

---

### 深度評析 (Synthesis & Critique)
- **核心貢獻**: Demonstrates quantitatively that fill simulation without adverse selection is unreliable. Provides a calibrated joint model that can be adopted in backtesting frameworks.
- **對 HFT 的啟示**: Validates our fill probability model in hft_native_runner.py. The adverse fill clustering on one side motivates the adverse_flow_asymmetry alpha: when fills consistently cluster on the buy or sell side, it indicates informed flow and we should trade in that direction.

---

### 行動清單 (Action Items)
- [ ] Implement `adverse_flow_asymmetry` alpha: detect asymmetry in fill-side clustering as informed flow signal
- [ ] Validate adverse selection modeling in hft_native_runner.py against this paper's framework
- [ ] Consider calibrating fill probability model with TWSE-specific adverse selection parameters
