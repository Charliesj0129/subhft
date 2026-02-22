# Graph Learning for FX Arbitrage and Prediction

**Authors**: Yoonsik Hong, Diego Klabjan (Northwestern University)
**Date**: Aug 2025
**Topic**: Graph Neural Networks (GNN), FX, Statistical Arbitrage, Interest Rate Parity

## Summary

The paper proposes a two-step Graph Learning (GL) approach for **FX Rate Prediction (FXRP)** and **FX Statistical Arbitrage (FXSA)**. It addresses two gaps: the lack of graph methods leveraging multi-currency/interest-rate relationships, and the neglect of "observation-execution time lag" in prior arbitrage studies. The method formulates FX trading as an edge-level regression on a spatiotemporal graph (Currencies = Nodes, Exchange Rates = Edges) and solves a stochastic optimization problem for arbitrage execution using a projection-based GNN.

## Key Concepts

### 1. FXRP (Prediction)

- **Graph Structure**: Nodes are currencies, Edges are exchange rates.
- **Features**:
  - **Node Features**: Interest Rates ($Y_{t,i}$), "Currency Value" derived from MLE ($V_t$).
  - **Edge Features**: Log-returns of FX rates.
- **Model**: A GNN that aggregates neighbor information (e.g., "how does a change in EUR/USD and USD/JPY affect EUR/JPY?").
- **Innovation**: Explicitly incorporates **Interest Rate Parity (IRP)** logic into the feature engineering.

### 2. FXSA (Arbitrage Execution)

- **Problem**: Triangular arbitrage ($A \to B \to C \to A$) is theoretically riskless but practically risky due to the time lag between spotting the arb and executing it ($\Delta t$).
- **Stochastic Optimization**: Maximizes the **Sharpe/Information Ratio** of the arbitrage PnL, constrained by flow conservation (net position in intermediate currencies must be 0).
- **Execution Model**: A second GNN ($f_S$) predicts the optimal trading quantities $w_{ij}$.
  - **Constraint Satisfaction**: Uses a projection layer ($h_{SO}$) to strictly enforce "flow-in = flow-out" constraints, ensuring no unintentional positions are held overnight.

### 3. Performance

- **Data**: 10 major currencies.
- **Results**:
  - **Prediction**: Statistically significant MSE improvement over baselines (LSTM, Transformer) by using the graph structure.
  - **Arbitrage**: +61.89% higher Information Ratio and +45.51% higher Sortino Ratio than benchmarks.
  - **Risk**: The method effectively hedges the "execution lag risk".

## Implications for Our Platform

- **Multi-Currency Alpha**: We can implement a similar **Currency Graph** in our `hftbacktest` environment to capture cross-pair correlations.
- **Execution Logic**: The projection layer idea ("projecting unconstrained GNN outputs onto the valid subspace of flow-conservation") is very powerful for any **portfolio optimization** task where weights must sum to 1 or neutralize exposure.
- **IR Data**: We should ensure our `market_data` service ingests real-time government bond yields or swap rates to power these IRP-based features.

## Tags

#GNN #FX #Arbitrage #Microstructure #Optimization #GraphLearning
