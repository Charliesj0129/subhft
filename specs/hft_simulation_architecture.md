# HFT Simulation Architecture: The "Embedded Digital Twin" (Lightweight & Integrated)

## 1. Overview

Instead of a distributed system (which introduces IPC overhead and deployment complexity), we implement the Simulator as a **Zero-Overhead Rust Module** embedded directly within the strategy process.
This ensures:

1.  **Microsecond Precision**: No serialization/network latency between Strategy and Sim.
2.  **Shared Memory**: The Simulator accesses the same LOB memory layout as the Strategy.
3.  **Single Binary**: Simplifies deployment and CI/CD.

## 2. Core Architecture (Rust)

### A. The `ExecutionGateway` Trait

We define a unified interface for order entry.

```rust
pub trait ExecutionGateway {
    fn place_order(&mut self, order: OrderRequest) -> OrderId;
    fn cancel_order(&mut self, order_id: OrderId) -> bool;
    fn on_tick(&mut self, tick: &Tick); // Drives the simulation
}
```

### B. The `SimulatedExchange` Struct (Impl `ExecutionGateway`)

This struct maintains the "Physics State" internally.

- **State**: `HashMap<OrderId, OrderState>`, `VecDeque<Order>` (Queue).
- **Physics**:
  - **OPI (Order Position Indicator)**: Tracks liquidity ahead of us in the LOB.
  - **Latency Buffer**: A `BTreeSet` of pending network events (Ack, Fill) scheduled for future timestamps (`Now + Latency`).
  - **Shadow Ledger**: Atomic `i64` counters for PnL and Fees.

### C. Python Interface (`PyGateway`)

A PyO3 wrapper that allows Python to drive the Rust simulator step-by-step.

```python
sim = rust_core.SimulatedExchange(latency_model="gamma", mean=5000) # 5ms
gateway.on_tick(tick) # Updates LOB and triggers Sim matching
```

## 3. Workflow Comparison

| Feature        | Live Trading        | Embedded Sim (New)       | Old Backtest             |
| :------------- | :------------------ | :----------------------- | :----------------------- |
| **Connection** | Shioaji API (TCP)   | Direct Memory Call       | Vectorized / Event Loop  |
| **Latency**    | Real Network        | `LatencyModel` (Sampled) | Constant / None          |
| **Matching**   | TSE Matching Engine | `OrderQueue` (FIFO)      | Probability / Last Trade |
| **Accounting** | Broker DB           | `ShadowLedger` (Rust)    | Estimated Simple PnL     |
| **Impact**     | Real Impact         | Simulated `QueueDecay`   | None                     |

## 4. Implementation Plan

1.  **Rust**: Define `ExecutionGateway` trait in `src/gateway.rs`.
2.  **Rust**: Implement `SimulatedExchange` struct in `src/sim.rs`.
3.  **Python**: Update `Strategy` to accept a `Gateway` injector (Dependency Injection).

## 5. Integration Diagram (Mermaid)

```mermaid
graph TD
    subgraph "Python Process (Zero-IPC)"
        Strategy[MakerStrategy.py]

        subgraph "Interface Layer"
            GatewayABC[<< Abstract >>\nExecutionGateway]
        end

        subgraph "Implementations"
            LiveAdapt[ShioajiAdapter\n(Live Mode)]
            SimAdapt[RustSimAdapter\n(Sim Mode)]
        end

        Strategy -->|1. place_order| GatewayABC
        GatewayABC -.->|Polymorphism| LiveAdapt
        GatewayABC -.->|Polymorphism| SimAdapt

        LiveAdapt -->|TCP| ShioajiAPI[Shioaji REST/WS]

        subgraph "Rust Core (Shared Library)"
            SimAdapt -->|PyO3 / FFI| RustSim[SimulatedExchange\n(Struct)]

            RustSim -->|Maintains| Ledger[ShadowLedger\n(Accounting)]
            RustSim -->|Maintains| Queue[OrderQueue\n(Matching Engine)]

            LOB[LimitOrderBook] -->|Market Data| RustSim

            Queue -->|Fill Event| Ledger
            Queue -->|Ack/Fill| RustSim
        end
    end

    ShioajiAPI -->|Real Exchange| TWSE[TWSE/TAIFEX]
```
