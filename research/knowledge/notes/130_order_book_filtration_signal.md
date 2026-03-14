# Order Book Filtration and Directional Signal Extraction at High Frequency
ref: 130
arxiv: https://arxiv.org/abs/2507.22712
Authors: (not specified)
Published: 2025

## 深度學術論文筆記 (Deep Academic Note)

### 基礎元數據 (Metadata)
- **標題**： Order Book Filtration and Directional Signal Extraction at High Frequency
- **作者**： (not specified)
- **年份**： 2025
- **期刊/會議**： ArXiv:2507.22712
- **關鍵詞**： #order_book #signal_extraction #obi #filtration #microstructure
- **閱讀狀態**： 已完成
- **關聯項目**： [[Alpha_Factor_Engineering]], [[Toxic_Flow]]

---

### 研究背景與目標 (Context & Objectives)
- **Research Gap**:
Order Book Imbalance (OBI) computed on raw event streams is noisy due to flickering liquidity — orders that are placed and canceled rapidly without genuine trading intent. This noise degrades directional signal quality.

- **研究目的**:
Propose and evaluate three filtration schemes to clean order book data before computing OBI, improving directional signal extraction at high frequency.

---

### 研究方法論 (Methodology)
- **Three Filtration Schemes**:
  1. Order lifetime filtering — exclude orders that exist for less than a threshold duration
  2. Modification count filtering — retain only orders that have been modified multiple times (indicating genuine intent)
  3. Modification timing filtering — weight orders by the timing pattern of their modifications
- **Signal Construction**: After filtration, compute OBI on the cleaned book to extract directional signals.

---

### 結果與討論 (Results & Discussion)
- Filtering parent orders of executed trades yields the strongest directional OBI signal.
- The improvement on aggregate flow metrics is modest but consistent.
- Filtration is most effective in high-activity periods where flickering liquidity is prevalent.

---

### 深度評析 (Synthesis & Critique)
- **核心貢獻**: Provides a principled framework for cleaning order book data before signal computation. Demonstrates that not all order book events carry equal information.
- **對 HFT 的啟示**: For TWSE L1 data, we cannot directly apply these filtration schemes (requires L3 order-level data). However, the paper validates our EMA-based smoothing approach as an approximation of persistence-weighting. This informs the design of toxicity_timescale_divergence where fast/slow EMA divergence proxies for order persistence.

---

### 行動清單 (Action Items)
- [ ] Reference only — informs toxicity_timescale_divergence design rationale
- [ ] Consider persistence-weighted OBI if L3 data becomes available for TWSE
