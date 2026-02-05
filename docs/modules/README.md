# Module Reference

This directory contains per-module documentation for the code under `src/hft_platform/`.

## Index
- `docs/modules/backtest.md` - Backtest runner, adapters, reporting
- `docs/modules/cli.md` - CLI entrypoints and helper commands
- `docs/modules/config.md` - Config loading and override rules
- `docs/modules/contracts.md` - Shared strategy/execution contracts
- `docs/modules/core.md` - Core abstractions and shared types
- `docs/modules/engine.md` - Event bus and engine plumbing
- `docs/modules/events.md` - Market data event structures
- `docs/modules/execution.md` - Execution normalization and positions
- `docs/modules/features.md` - Feature library and indicators
- `docs/modules/feed_adapter.md` - Feed adapters and normalizers
- `docs/modules/ipc.md` - Shared memory / ring buffer utilities
- `docs/modules/main.md` - Application entrypoint
- `docs/modules/observability.md` - Metrics and logging
- `docs/modules/order.md` - Order adapter and routing
- `docs/modules/recorder.md` - WAL, ClickHouse writer, replay
- `docs/modules/risk.md` - Risk engine and validators
- `docs/modules/schemas.md` - Database schemas
- `docs/modules/services.md` - System services and supervisor
- `docs/modules/strategies.md` - Built-in strategy implementations
- `docs/modules/strategy.md` - Strategy SDK and routing
- `docs/modules/utils.md` - Utilities and helpers

## Template
Use this outline for new module docs:
- Purpose
- Entry points
- Inputs/outputs
- Constraints
