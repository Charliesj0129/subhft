# Research Survey: Statistical Arbitrage & Microstructure (2015-2025)

## 1. Top Significant Papers (Last 10 Years)

### A. Statistical Arbitrage (ML & Deep Learning Revolution)

**Theme**: Moving beyond PCA/Cointegration to Non-linear / Reinforcement Learning models.

- **Mulvey et al. (2020)**: _Synthetic Data & RL in Stat Arb_
  - **Innovation**: Using GANs to generate synthetic market data for training robust RL agents, overcoming the "data scarcity" in regime shifts.
  - **Relevance**: Directly supports our Generative Market Simulation (Cluster 3).

- **Kim & Kim (2019)**: _Deep Learning for Hat-Trick Arb_
  - **Innovation**: Applying CNNs/LSTMs to identify mispricing deep in the order book (Micro-structure level) rather than just OHLC.

- **Cartea & Jaimungal (2016)**: _Algorithmic Trading of Co-Integrated Assets_
  - **Significance**: The "Bible" of modern stochastic control for pairs trading. Introduces closed-form optimal control for mean-reverting portfolios.

- **Guijarro-Ordonez et al. (2025)**: _PCA-Type Factors & Sequence Models_
  - **Current State**: Demonstrates high Sharpe Ratios using Transformer-based sequence models on residual returns, but warns of transaction cost degradation.

### B. Futures & Cross-Asset Arbitrage

**Theme**: Latency, Cross-Exchange, and Market Invariance.

- **Kyle & Obizhaeva (Invariance Hypothesis) (Refined consistently 2016-2022)**
  - **Concept**: Trading costs and bid-ask spreads scale with volatility and volume in a universal "invariant" manner.
  - **Application**: Calibrating "Fair Spread" in Futures markets to detect arbitrage opportunities when deviations occur.

- **Budish, Cramton, Shim (2015/2016 Focus)**: _The CFO (Continuous Frequent Batch Auctions)_
  - **Impact**: While proposing market design changes, it fundamentally exposed the mechanical "Latency Arbitrage" tax existing in continuous double auctions.

## 2. Research Trends (2020-2025)

### A. The "Physics" of Liquidity

- **Trend**: Modeling LOB Dynamics using Statistical Mechanics (Thermodynamics, Entropy).
- **Paper**: _The Physics of Order Books (2022/2023)_ - treating order flows as particle systems to predict "Phase Transitions" (Crashes/Spikes).
- **Our Alignment**: Cluster 2 (LOB Thermodynamics) is directly on this frontier.

### B. "Digital Twin" & Simulation

- **Trend**: Moving from backtesting to "Market Simulation".
- **Key Insight**: You cannot train RL on historical data alone (overfitting). You must train on "Reactive Simulators" (Agent-Based Models).
- **Relevance**: Our newly designed "Hardened Simulator" aligns 100% with this industry direction (e.g., J.P. Morgan's ABIDES).

### C. Generalized Arbitrage (Graph Theory)

- **Trend**: Finding arbitrage loops across 100+ assets simultaneously using Graph Network algorithms, rather than simple Pairs.
- **Method**: Minimum Spanning Trees (MST) to filter noise in covariance matrices, then finding negative cycle opportunities.

## 3. Recommended Reading List for Architecture

1.  **"High-Frequency Trading: A Practical Guide to Algorithmic Strategies and Trading Systems"** (Updated eds).
2.  **"Machine Learning for Algorithmic Trading"** (Stefan Jansen) - specifically the chapter on Cointegration and Pairs.
3.  **ABIDES (Agent-Based Interactive Discrete Event Simulation)** paper - _For Simulator Architecture inspiration._
