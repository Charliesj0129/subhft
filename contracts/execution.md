# Execution & Positions Contracts

## Data Structures

### OrderEvent
Tracks lifecycle of an order.
- `status`: PENDING -> SUBMITTED -> FILLING -> FILLED/CANCELLED
- `ordno`: Broker Order No.

### FillEvent
Represents a trade match.
- `price`: Executed price.
- `fee`/`tax`: Cost basis adjustments.

### PositionDelta
 emitted by PositionStore on any change.
- `realized_pnl`: Closed PnL.
- `unrealized_pnl`: Mark-to-Market PnL (updated via market data ticks).

## reconciliation
- **Startup**: Poll broker snapshot.
- **Runtime**: Heartbeat check -> poll if gap.
