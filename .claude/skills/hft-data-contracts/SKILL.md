---
name: hft-data-contracts
description: Field reference for HFT platform data contracts — OrderIntent, OrderCommand, FillEvent, PositionDelta, TickEvent, BidAskEvent, LOBStatsEvent. Use when designing a new strategy, modifying risk/execution code, writing tests that construct these events, or reviewing PRs that touch `contracts/strategy.py`, `contracts/execution.py`, or `events.py`. All price fields are scaled int (x10000).
---

# Key Data Contracts (Scaled Int Convention)

> Quick summary in CLAUDE.md (repo root, `## 📦 Key Data Contracts`). Full field reference below.

All price fields are `int` scaled by **x10000** (configurable per symbol in `symbols.yaml`).

```
OrderIntent → (Risk) → RiskDecision → OrderCommand → (Broker) → FillEvent → PositionDelta
```

| Contract        | File                     | Key Fields                                                          |
| --------------- | ------------------------ | ------------------------------------------------------------------- |
| `OrderIntent`   | `contracts/strategy.py`  | `price: int`, `qty: int`, `side: Side`, `idempotency_key`, `ttl_ns` |
| `OrderCommand`  | `contracts/strategy.py`  | `cmd_id`, `deadline_ns`, `storm_guard_state`                        |
| `FillEvent`     | `contracts/execution.py` | `price: int`, `fee: int`, `tax: int` (all x10000)                   |
| `PositionDelta` | `contracts/execution.py` | `net_qty`, `avg_price: int`, `realized_pnl: int`                    |
| `TickEvent`     | `events.py`              | `price: int` (x10000), `volume`, `meta: MetaData`                   |
| `BidAskEvent`   | `events.py`              | `bids/asks: np.ndarray` shape (N,2), `stats: tuple`                 |
| `LOBStatsEvent` | `events.py`              | `mid_price_x2: int`, `spread_scaled: int`, `imbalance: float`       |
