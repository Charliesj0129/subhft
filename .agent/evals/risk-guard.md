# Eval: Risk Guard

**Component**: `src/hft_platform/risk/`

## Capability

### C1: Pre-Trade Validation
- Validates every order before submission against position limits.
- Checks: max order size, max position, max notional exposure.
- Returns accept/reject decision with reason.

### C2: Rate Limiting
- Enforces maximum orders per second / per minute.
- Rejects orders that exceed rate limits with clear error code.

### C3: Storm Guard
- Detects abnormal market conditions (price gap, volume spike).
- Can pause or reduce order flow automatically.
- Configurable thresholds per instrument.

### C4: Position Tracking
- Maintains real-time position state per instrument.
- Updates on fill events, not on order submission.
- Handles partial fills correctly.

### C5: Kill Switch
- Provides emergency kill switch to cancel all open orders.
- Can be triggered programmatically or via config flag.
- Must complete within 1ms of invocation.

## Regression

### R1: Latency
- **Pre-trade check**: < 10us mean per validation.
- **Position update**: < 5us mean per fill event.
- Must not block the order submission path.

### R2: Correctness
- No order must bypass risk checks (100% enforcement).
- Position limits must be exact (no off-by-one on boundary).
- Rate limiter must use monotonic clock, not wall clock.

### R3: Precision
- All monetary values use integer micros or Decimal.
- No float arithmetic in limit comparisons.
- Notional calculations must be exact.
