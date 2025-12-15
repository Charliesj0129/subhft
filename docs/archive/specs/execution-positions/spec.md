# Execution Events & Position State

## Problem Statement
Provide a low-latency execution plane that ingests Shioaji order/deal callbacks, normalizes them into internal `OrderEvent` / `FillEvent` structures, updates an in-memory `PositionStore`, and fans updates to StrategyRunner, Risk, and downstream consumers. The system must treat the broker as the source of truth, reconcile state on restart within 5–10 s, and expose accurate positions (with realized/unrealized PnL and fees) to risk within ≤10 ms of a fill. It also needs to feed the cold path (Async Recorder → ClickHouse) and future dashboards/APIs.

## Context & Actors
- **Shioaji Order/Deal Callbacks** – primary real-time feed for order status and fills; `update_status` polling used only for reconciliation.
- **Execution Normalizer** – decodes callbacks, maps to internal structs (`OrderEvent`, `FillEvent`), stamps timestamps, and pushes onto event bus / recorder queue.
- **PositionStore** – in-memory state keyed by account/symbol/strategy, tracking quantities, average price, realized/unrealized PnL, fees, and exposure.
- **StrategyRunner & Risk** – consume execution events to adjust outstanding orders, PnL, and StormGuard states.
- **Async Recorder / ClickHouse** – persist orders/fills/positions/account snapshots for audit/backtest.
- **Ops/Dashboards** – future consumers requiring read-only API snapshots.

## Goals & Success Criteria
1. Capture every broker execution event via callbacks with deterministic ordering; if callback gap detected, trigger reconciliation poll.
2. Normalized events include broker IDs (`ordno`, `seqno`), timestamps (broker + ingest), intent linkage (`strategy_id`, `intent_id`), and fee/commission details when available.
3. PositionStore updates and downstream notifications complete within ≤10 ms of receiving a fill; StrategyRunner/Risk see consistent state before processing next event.
4. Restart recovery path: bootstrap positions/orders via broker snapshot APIs (`list_positions`, `list_profit_loss`, etc.), optionally cross-check with ClickHouse logs, and resume normal operation within 5–10 s.
5. Periodic snapshots (e.g., every 1 s) emitted to Async Recorder for ClickHouse tables (`orders`, `fills`, `positions`, `account_state`) alongside high-frequency deltas.

## Scope
- Subscription to Shioaji order/deal callbacks, including registration, threading, and GIL considerations.
- Conversion to internal execution event schema; deduplication and ordering.
- Position management per account/symbol/strategy including PnL/fees.
- Snapshotting and delta emission to in-process bus and recorder.
- Recovery/reconciliation workflow leveraging broker APIs and optional log replay.

## Out of Scope
- Order placement/amend/cancel logic (handled by Strategy/Risk/Order module).
- Risk guardrails themselves (they consume the outputs).
- Visualization/dashboard rendering (future work).

## Detailed Requirements
### Execution Feed Handling
- Register Shioaji callbacks (`api.on_order`, `api.on_deal`) similar to market data; callbacks run on Shioaji’s threads, so keep handler minimal: decode payload, capture ingest timestamp, enqueue for normalization.
- Poll `api.update_status` / `list_positions` at controlled cadence for reconciliation (e.g., on startup, and if heartbeat indicates missed callback).
- Handle order lifecycle states per Shioaji (`PendingSubmit`, `Submitted`, `Filling`, `Filled`, `Cancelled`, `Failed`); map to internal enums.
- Each `FillEvent` must include:
  - `order_id` / `ordno` / `seqno`
  - `symbol`, `side`, `qty`, `price`
  - `match_ts` (broker), `ingest_ts`
  - `fees`, `tax`, `rebates`
  - `strategy_id` link (from order metadata)
- Deduplicate duplicate callbacks via `seqno` + `match_ts`.

### PositionStore
- Keyed by `(account_id, strategy_id, symbol)` with aggregated views per account and per symbol.
- Tracks:
  - `position_qty` (net)
  - `avg_price` (weighted by fills)
  - `realized_pnl`
  - `unrealized_pnl` (mark-to-market using LOB mid or last trade)
  - `fees`/`taxes`
  - `exposure_notional`
  - `last_update_ts`
- Provides:
  - High-frequency delta events (`PositionDelta`) on each change (fill/cancel affecting outstanding).
  - Periodic snapshots (configurable interval) for risk dashboards and recorder.
  - Query API for StrategyRunner/Risk to fetch latest state without copy (shared memory or pointer).
- On StormGuard HALT or cancel-all, PositionStore can verify flat status and signal readiness to resume.

### Recovery & Reconciliation
- **Startup**:
  1. Set state to `RECOVERING`.
  2. Call broker snapshot APIs (`list_positions`, `list_profit_loss`, `list_settlements`, etc.) to fetch ground-truth orders/positions.
  3. Reset PositionStore and outstanding-order maps to match broker data.
  4. Optionally replay latest ClickHouse logs (orders/fills) for auditing differences; log discrepancy if mismatch vs broker snapshot.
  5. Once aligned, transition to `LIVE`.
- **Runtime reconciliation**:
  - Heartbeat monitors ensure callbacks are flowing; missing updates for >X seconds triggers snapshot poll to detect missing fills.
  - If discrepancies found (e.g., broker shows more filled qty than PositionStore), generate correction events and update risk/strategies.
- Recovery SLA: 5–10 s to re-sync after restart, ensuring risk sees accurate exposures before trading resumes.

### Integration & Exports
- Execution events posted to in-process event bus so strategies can update their order state machines.
- Position updates exposed via shared memory snapshot and gRPC/CLI for ops.
- Async Recorder receives:
  - Real-time deltas (orders/fills) for ClickHouse ingestion.
  - Periodic snapshots (positions/account) to maintain historical ledger.
- Future: Provide read-only API/dashboards that read from PositionStore or ClickHouse for monitoring.

### SLA & Non-Functional
- **Latency**: ≤10 ms from callback receipt to risk visibility; average target <2 ms.
- **Determinism**: Apply execution events in order they are received; deterministic rebuild using broker snapshots.
- **Reliability**: Detect and alert on callback gaps, reconciliation failures, or mismatches with broker totals.
- **Auditability**: Every update includes source (callback vs snapshot), version, and reason codes for adjustments.
- **Security**: Broker data handled in-memory; no sensitive account details logged beyond IDs needed for trace.

## Main Flows
1. **Normal Execution**: Callback → normalize → PositionStore update → Strategy/Risk/Recorder notifications → metrics/log.
2. **Startup Recovery**: Snapshot → rebuild positions/orders → emit snapshot events → switch to callback mode.
3. **Discrepancy Handling**: Heartbeat detects missed fill → poll snapshot → compute delta → adjust PositionStore → issue correction events.
4. **Snapshot Emission**: Timer triggers periodic position/account snapshot emission to recorder/dashboards.

## Edge Cases
- Duplicate fills (e.g., broker replays) – dedup via `seqno` and `match_ts`.
- Partial cancel after fill – adjust outstanding quantity accordingly.
- Odd-lot vs regular – maintain separate position buckets if needed.
- Multi-account trading – PositionStore must support multiple accounts per broker credentials.
- Day change / settlement – reset realized PnL counters per exchange rules while retaining historical data.

## References
- `sinotrade_tutor_md/order_deal_event` – details on callback payload fields and lifecycle.
- `sinotrade_tutor_md/order/*.md` – order states and API usage for reconciliation.
- `sinotrade_tutor_md/limit.md` – polling limits for portfolio queries (25 calls / 5 s) to respect during recovery.

## Assumptions & Open Questions
- **Assumption**: Broker callbacks are authoritative; polling only corrects missed messages, not primary feed.
- **Assumption**: Position attribution by strategy available (orders carry `strategy_id` in custom fields).
- **Open**: Need precise mapping of fees/taxes per product; confirm Shioaji payloads include them or require lookup.
