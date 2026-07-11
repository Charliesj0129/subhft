# HFT Simulation Architecture: The "Hardened Digital Twin" (Latency-Accurate & Fault-Isolated)

## 1. Response to Critical Review

We acknowledge the weaknesses in the "Embedded" design. This validated architecture addresses them directly:

| Risk                     | Mitigation Strategy                                                                                                                                         |
| :----------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **No Fault Isolation**   | **Thread Isolation**: Simulator runs in a dedicated Rust thread. Strategy & Sim communicate **only** via SPSC Lock-Free Ring Buffers (mimicking NIC RX/TX). |
| **Hot-Path Allocations** | **Arena Allocation**: `Slab<Order>` + `FixedSizeVec` for LOB levels. **Zero Malloc** in the tick loop.                                                      |
| **Cache Locality**       | **SoA Layout**: LOB and Queue state are packed in Structure-of-Arrays (SoA) for SIMD-friendly traversal.                                                    |
| **Time Indeterminism**   | **Causal Clock**: Time advances **only** on Market Data events. No `std::time::Instant::now()`.                                                             |
| **GIL Overhead**         | **Rust-Native Loop**: The Physics Engine runs 100% in Rust. Python receives batched asynchronous reports.                                                   |
| **Optimistic Matching**  | **Volume-Time Priority**: Explicit $Q_{pos}$ tracking. Cancels decay queue position conservatively (Worst-Case).                                            |
| **Single-Binary Bias**   | **Artificial Friction**: The Ring Buffer explicitly injects "Serialization Cost" (CPU Burn) and "Network Jitter".                                           |

## 2. System Topology: "The Ring-Fenced Simulator"

```mermaid
graph TD
    subgraph "Process Memory Space"
        subgraph "Strategy Thread (Python/Rust)"
            Strategy[Alpha Strategy]
            GW[Execution Gateway]
            GW -->|Push Order| TX_Ring[SPSC RingBuffer (TX)]
        end

        subgraph "Simulation Thread (Rust)"
            RX_Ring[SPSC RingBuffer (RX)] -->|Pop Event| PhysEngine[Physics Engine]

            PhysEngine -->|Update| Matcher[Matching Core]
            PhysEngine -->|Update| Risk[Risk & Ledger]

            Matcher -->|Fill/Ack| TX_Sim_Ring[SPSC RingBuffer (Sim->Strat)]
        end

        TX_Ring -.->|Zero Copy| RX_Ring
        TX_Sim_Ring -.->|Zero Copy| GW
    end
```

## 3. Component Details (Rust)

### A. The Physics Engine (`SimThread`)

- **Isolation**: Spawns a dedicated OS thread pinned to a separate Core (if available).
- **Input**:
  1.  **Market Data Feed**: Shared read-only access (or duplicate stream) to Tick Data.
  2.  **Order Commands**: Consumed from `TX_Ring`.
- **Clock**: `CurrentTime = Tick.ExchangeTimestamp + LatencyProfile`.

### B. The Memory Model (Allocator Law Compliant)

Instead of `HashMap<OrderId, OrderState>`, we use **Generational Indices**.

- **Storage**: `Vec<OrderSlot>` pre-allocated at startup (e.g., 100k slots).
- **Access**: `OrderId` is a `u64` encoding `(Index, Generation)`. O(1) access, perfect cache locality.
- **Queue**: `IntrusiveLinkedList` backed by the same Arena. Nodes are indices.

### C. The Matching Engine (Conservative Physics)

- **Queue Logic**:
  - $Q_{pos}$ is tracked for every Limit Order.
  - **Trade Event**: Decrements $Q_{pos}$ by `Trade.Volume`. If $Q_{pos} \le 0$, Execute.
  - **Cancel Event**:
    - _Naive_: $Q_{pos}$ unchanged (Pessimistic - assume cancel was behind us).
    - _Realistic_: $Q_{pos} -= CancelVol * (Q_{pos} / TotalVol)$ (Proportional Decay).
- **Self-Impact**: Simulates the removal of liquidity. If we execute, we _virtually_ remove that volume from the LOB state used for subsequent matches within the same millisecond.

### D. Network Emulation (`NetModel`)

- **Latency Injection**:
  - We load `latency_hist.csv` (Real-world RTTs).
  - Each message in RingBuffer acts as if it has a "Hardware Timestamp".
  - `Delay = Sample(LatencyProfile) + CongestionPenalty`.
  - **Congestion**: If RingBuffer usage > 50%, add exponential backoff delay (simulating TCP Window scaling).

## 4. Workflows

### Phase 1: Pre-Allocation (Init)

- User defines `MaxOrders=100_000`.
- Sim thread allocates explicit 64MB Arena.
- Ring Buffers initialized (Power of 2 size).

### Phase 2: The Loop

1.  **Strategy**: Computes Alpha -> Calls `gateway.submit()`.
    - Gateway writes `OrderRequest` to RingBuffer. **Returns immediately (Async)**.
2.  **Simulation**:
    - Reads `OrderRequest`.
    - Calculates `ArrivalTs = Now + OneWayLatency`.
    - Inserts into `PendingQueue` (PriorityQueue sorted by Ts).
3.  **Physics Tick**:
    - Advances `SimTime` to `NextMarketTick.Ts`.
    - Processes all `PendingQueue` items where `ArrivalTs <= SimTime`.
    - Runs Matcher -> Generates `FillEvent`.
    - Writes `FillEvent` to Output RingBuffer with `ArrivalTs = SimTime + OneWayLatency`.

## 5. Verification

- **Leak Check**: Use `valgrind` / `metrics` to ensure 0 allocs in `sim_loop`.
- **Stall detection**: If RingBuffer full, Strategy gets `WouldBlock` error (Simulating dropped packet or backpressure).
