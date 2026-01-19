# order

## Purpose
Order routing, rate limiting, and broker interaction logic.

## Key Files
- `src/hft_platform/order/adapter.py`: OrderAdapter (new/amend/cancel lifecycle).
- `src/hft_platform/order/rate_limiter.py`: Sliding window limiter.
- `src/hft_platform/order/circuit_breaker.py`: Failure guard.

## Flow
1) Receive `OrderCommand` from Risk Engine.
2) Validate deadline and rate limits.
3) Place or update broker orders.
4) Track live orders and broker IDs.

## Configuration
- `config/order_adapter.yaml`
- Rate limits and circuit breaker thresholds.

## Extension Points
- Add broker-specific adapters or routing policies.
