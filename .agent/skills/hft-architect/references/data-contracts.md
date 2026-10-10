# Data contracts

Read when you build tests with these types or review a diff touching
`contracts/strategy.py`, `contracts/execution.py`, or `events.py` (Do-NOT-Edit
without explicit instruction). All price fields are `int` scaled by x10000.

```
OrderIntent -> (Risk) -> RiskDecision -> OrderCommand -> (Broker) -> FillEvent -> PositionDelta
```

| Contract | File | Key fields |
|---|---|---|
| `OrderIntent` | `contracts/strategy.py` | `intent_id`, `strategy_id`, `symbol`, `intent_type`, `side`, `price: int`, `qty`, `tif`, `idempotency_key`, `ttl_ns`, `decision_price`, `trace_id` |
| `RiskDecision` | `contracts/strategy.py` | `approved`, `intent`, `reason_code`, `modified` |
| `RiskFeedback` | `contracts/strategy.py` | `intent_id`, `reason_code`, `was_approved` |
| `OrderCommand` | `contracts/strategy.py` | `cmd_id`, `intent`, `deadline_ns` (monotonic), `storm_guard_state`, `arrival_price` |
| `OrderEvent` | `contracts/execution.py` | `order_id`, `status`, `submitted_qty`, `filled_qty`, `remaining_qty`, `client_order_id` |
| `FillEvent` | `contracts/execution.py` | `fill_id`, `order_id`, `price`, `fee`, `tax` (all x10000), `match_ts_ns`, `client_order_id` |
| `PositionDelta` | `contracts/execution.py` | `net_qty`, `avg_price`, `realized_pnl`, `unrealized_pnl`, `delta_source` |
| `TickEvent` / `BidAskEvent` | `events.py` | `price: int`, `volume`, `meta: MetaData`; bids/asks arrays (variable length 0-5) |
| `LOBStatsEvent` | `events.py` | `mid_price_x2: int`, `spread_scaled: int`, `imbalance: float` |

`StormGuardState` is `NORMAL / WARM / STORM / HALT`. Field lists drift: read
the file before relying on this table.
