# HFT Platform Features

The **HFT Platform** is a high-performance, Python-based trading system designed for **Micro-Alpha Research** and **Algorithmic Execution**. It bridges the gap between statistical research and production trading with a unified architecture.

## 🚀 Core Architecture
*   **Event-Driven Engine**: Low-latency `RingBufferBus` for lock-free event distribution.
*   **Production Hardened**:
    *   **Fault Tolerance**: Automatic reconnection, heartbeat monitoring, and WAL (Write-Ahead Log) for crash recovery.
    *   **Spec Compliance**: Fully validated against Shioaji API specifications for seamless integration.
*   **DataOps**:
    *   **Persistence**: Real-time generic WAL writer (JSONL) with automated ClickHouse loading.
    *   **Schema**: Optimized ClickHouse schema supporting LOB Arrays for depth analysis.

## 🧠 Advanced Alpha Engineering
Built-in support for state-of-the-art microstructure features (`src/hft_platform/features/`):

### 1. Information Theory
*   **Shannon Entropy**: Measures LOB liquidity concentration. High entropy = dispersed liquidity (uncertainty); Low entropy = wall formation.
*   **Earth Mover's Distance (EMD)**: Quantifies the "cost" to transform the previous LOB shape into the current one, detecting structural regime shifts.

### 2. Microstructure Signals (OFI)
*   **Integrated OFI**: Order Flow Imbalance calculation across multiple depth levels (Cont et al.).
*   **OFI Decomposition**: Breaks down flow into:
    *   **Limit**: Passive liquidity addition.
    *   **Cancel**: Liquidity removal.
    *   **Trade**: Aggressive volume consumption (Impact).

### 3. Fractal & Statistical Analysis
*   **Hurst Exponent**: R/S Analysis to classify market memory (Mean Reverting vs Trending).
*   **Roll Spread**: Covariance-based estimator of effective spread.
*   **Amihud Illiquidity**: Volume-normalized price impact metric.

### 4. Pricing Models
*   **Stoikov Micro-Price**: Imbalance-adjusted fair value estimation ($P_{micro} = P_{mid} + \delta \cdot I$).
*   **Kyle's Lambda**: Rolling regression of price changes against order flow.

## 🛡️ Risk Management
*   **Multi-Layer Validations**: `PriceBand`, `MaxNotional`, and `FatFinger` checks.
*   **Circuit Breakers**: "Storm Guard" state machine halts trading during extreme volatility or system instability.
*   **Pre-Trade Risk**: Checks performed *before* order submission to Broker.

## 🛠️ Developer Experience (DX)
*   **Strategy SDK**:
    *   Lifecycle hooks (`on_tick`, `on_order`, `on_fill`).
    *   Private event routing (fills go only to the originating strategy).
    *   User-friendly wrappers for complex features (e.g., `factors.price_entropy(lob)`).
*   **Unified CLI**: `hft_platform` command for simulation (`sim`), live trading (`live`), and backtesting (`backtest`).
*   **Backtesting**:
    *   Integration with `hftbacktest` for high-fidelity simulation.
    *   Mock data generation for strategy prototyping.

## 🔍 Observability
*   **Prometheus Metrics**: Detailed instrumentation of Feed Latency, Order Throughput, Fill Rates, and Feature Calculation time.
*   **Structured Logging**: JSON-structured logs via `structlog`.
