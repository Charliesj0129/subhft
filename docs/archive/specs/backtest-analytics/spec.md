# Backtest & Analytics

## Problem Statement
Leverage the ClickHouse cold path to provide both (a) live-like backtests that replay tick/L2 data through the same strategy/risk stack with a simulated broker, and (b) analytics workflows for factor research, behavior analysis, and reporting. The system must stream historical data in manageable chunks, support configurable latency/slippage models, persist structured run outputs for dashboards, and ensure all data remains within internal infrastructure.

## Scope
- Backtest runtime that reuses `StrategyRunner`, `RiskEngine`, `PositionStore`, etc., swapping live connectors for offline equivalents (ClickHouse replay feed, simulated broker).
- Latency/slippage models (configurable) applied to replayed events and order execution.
- Data access layer streaming from ClickHouse (`market_data`, `orders`, `fills`, `positions_intraday`, etc.) with optional exports to Parquet/Arrow for repeated research.
- Analytics outputs persisted in ClickHouse (`backtest_runs`, `backtest_timeseries`, derived views).
- Tooling (Python APIs, CLI, notebooks) for running backtests and analyzing results.

## Out of Scope
- Real-time monitoring (handled by observability slice).
- External SaaS analytics; all processing stays on internal infra unless data is aggregated/anonymized.

## Backtest Modes & Goals
1. **Simulation mode (live-like)**:
   - Replay normalized market snapshots (tick/L2) from `market_data`.
   - Feed events into `StrategyRunner` → `Risk` → `SimulatedBroker` (backtest engine) with identical logic to live.
   - Apply configurable latency (constant + jitter) and L2 execution model (top-5 depth, queue consumption, passive fills when price crosses).
   - Produce execution/fill events compatible with live `PositionStore`.
2. **Analytics mode (research)**:
   - Operate on aggregated/derived tables (e.g., 1s/1m bars, feature tables, order statistics).
   - Support notebooks and automated reports for feature engineering, slippage studies, behavior analysis, PnL attribution.

## Data Flow & Access
- Primary source: direct queries to ClickHouse tables (`market_data`, `orders`, `fills`, `positions_intraday`, `account_state`, `risk_decisions`), filtered by symbol(s), date range, and source.
- Data streamed in time-ordered chunks (e.g., per symbol-day), not loaded entirely into RAM.
- Optional export path: ClickHouse → Parquet/Arrow for repeated runs or offline sharing; controlled by config to ensure data stays in internal storage.
- Compliance: only internal systems access raw data; any export/anonymization steps documented.

## Latency & Execution Modeling
- Latency model: baseline constant (0.5–3 ms) plus optional jitter (Gaussian or empirical). Apply to:
  - Feed arrival (simulate network delay).
  - Order arrival/execution lean time.
- Execution model:
  - Use top-5 LOB depth from `market_data` snapshots; fill aggressive orders by consuming queue across levels.
  - Passive orders fill when trades cross or queue empties to their price.
  - Support TIF behavior (IOC/FOK) consistent with live stack.
  - Optional advanced models (queue position, impact) for future versions.

## Tooling & Runtime
- Add `hft_platform.backtest` package containing:
  - `ClickHouseReplayFeed` – streams events in chronological order from CH.
  - `SimulatedBroker` – implements order execution using hftbacktest or in-house logic, returning fills/events.
  - `BacktestRunner` – orchestrates feed, strategy/risk stack, simulated broker, collects results.
  - CLI/Config files to specify strategy params, symbols, date ranges, latency/slippage profile.
- Support multi-process/parallel runs (e.g., parameter sweeps, symbol partitions) using multiprocessing/joblib/dask.
- Provide Jupyter notebooks (under `/notebooks` or `/research`) that import `BacktestRunner` for ad-hoc analysis.

## Analytics Outputs
- `backtest_runs` (ClickHouse table):
  - `run_id`, `strategy_name`, `config_hash`, `symbols`, `date_range`, `latency_profile`, `slippage_profile`.
  - Summary statistics: total PnL, annualized return, Sharpe, Sortino, max drawdown, win rate, payoff ratio, turnover, order volume %, runtime, git commit/version.
- `backtest_timeseries`:
  - `run_id`, `ts`, `equity_curve`, `drawdown`, `gross_exposure`, `net_exposure`, optionally `pnl_by_symbol` or separate tables keyed by symbol/strategy.
- Derived analytics (views or additional tables):
  - Order behavior (fill rate, slippage vs mid, cancel/replace stats) grouped by symbol, time of day, order type.
  - PnL attribution (by symbol, sector, factor bucket).
  - Factor/future research tables built from aggregated market data.
- Reports consumed via notebooks, Grafana/Superset dashboards, and optional static HTML exports.

## Performance Targets
- Single run should handle 10^6–10^8 events by streaming; avoid loading more than a few minutes of data into memory at once.
- Throughput: at least 10k–25k events/sec in simulation mode on target hardware to keep pace with live data volume.
- Latency: keep per-event processing deterministic (StrategyRunner budgets enforced) even in offline mode.

## Non-Functional Requirements
- Deterministic replay: event ordering must match historical sequence; random seeds captured for reproducibility.
- Config traceability: tie each run to strategy/risk configs, code version, and latency/slippage settings.
- Security: no raw data leaves internal environment; exports (if any) restricted to aggregated/approved formats.
- Observability: record run metrics (progress,  events/sec, memory use) for long-running backtests.

## Assumptions & Open Questions
- **Assumption**: ClickHouse cluster can support streaming queries for historical data.
- **Assumption**: `hftbacktest` (or equivalent) available for SimulatedBroker.
- **Open**: Define exact latency/slippage profiles per strategy; coordinate with trading team to calibrate vs live stats.
