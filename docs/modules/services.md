# services

## Purpose
Top-level orchestration and service wiring.

## Key Files
- `src/hft_platform/services/system.py`: `HFTSystem` service supervisor.
- `src/hft_platform/services/bootstrap.py`: Build service graph from settings.
- `src/hft_platform/services/registry.py`: Service registry.
- `src/hft_platform/services/market_data.py`: Market data pipeline.
- `src/hft_platform/services/execution.py`: Execution pipeline.

## Flow
1) `HFTSystem` initializes services via bootstrap.
2) Services run async loops and publish events.
3) Registry maintains shared references.

## Extension Points
- Add new services and register them in bootstrap.
- Expand system lifecycle hooks (start/stop).
