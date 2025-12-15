# Strategy & Risk Contracts

## Enums
- **StormGuardState**:
  - `NORMAL` (0): No restrictions.
  - `WARM` (1): Throttled order rates, price bands tightened.
  - `STORM` (2): Close-only mode (position increasing rejected).
  - `HALT` (3): Cancel-all, reject new, manual resume required.

- **IntentType**: NEW (0), AMEND (1), CANCEL (2).
- **TIF**: LIMIT (0), IOC (1), FOK (2).

## Data Structures

### OrderIntent
Core message passed from `StrategyRunner` -> `RiskEngine`.
```python
@dataclass(slots=True)
class OrderIntent:
    intent_id: int        # Monotonic ID
    strategy_id: str      # e.g., "STRAT_001"
    symbol: str           # "2330"
    intent_type: IntentType
    side: Side            # BUY/SELL
    price: int            # Fixed-point (x10000)
    qty: int              # Integer shares
    tif: TIF
    target_order_id: str  # Required for AMEND/CANCEL
    timestamp_ns: int     # Creation time
```

### OrderCommand
Message from `RiskEngine` -> `OrderAdapter`.
- Wraps an approved `OrderIntent`.
- Includes `deadline_ns` for latency budgeting.
- Carries snapshot of `storm_guard_state`.

## Flows
1. **Strategy** creates `OrderIntent`.
2. **Risk** validates:
    - If Approved -> Wraps in `OrderCommand`, pushes to Adapter Queue.
    - If Rejected -> Updates `reason`, signals Strategy via Feedback.
3. **Adapter** consumes `OrderCommand`:
    - Checks `deadline_ns` (drop if expired).
    - Maps to Shioaji API call.
