# R23 Stage 1 — Execution Review

**Date**: 2026-03-28
**Reviewer**: Execution Reviewer Agent
**Survey**: `docs/alpha-research/r23_stage1_survey.md` + researcher direct message
**Scope**: Tradability verification for 3 candidates

---

## Latency Profile Reference

Source: `config/research/latency_profiles.yaml` — `shioaji_sim_p95_v2026-03-04`

| Metric | Value |
|--------|-------|
| Internal pipeline (decision) | 250 us |
| Submit -> ack (P95) | 36 ms |
| Modify -> ack (P95) | 43 ms |
| Cancel -> ack (P95) | 47 ms |
| **Total signal-to-fill floor** | **~36.25 ms** |

Cost model: TMFD6 RT = 3.92 pts (1.19 bps). No maker rebates (retail).

---

## Candidate A: Regime-Conditional Intraday Trend Following — APPROVE

### Signal vs Latency — PASS (non-issue)

Hold period: 30min-4hr. At 36ms RTT, latency is 0.002% of a 30-min hold. Entry can use passive limit orders. Even market order exit is fine at these timescales. **No latency concern whatsoever.**

### Researcher Question: Can StrategyRunner handle multi-hour position management?

**YES.** Verified by code inspection:

1. **StrategyRunner** (`src/hft_platform/strategy/runner.py:57`) dispatches events to strategies via `BaseStrategy.handle_event()`. It is event-driven, not poll-driven. There is NO assumption of tick-level responsiveness. Strategies receive events and decide what to do. No timers, no mandatory response cadence.

2. **Precedent: CBS already manages multi-minute holds.** `CascadeBounceStrategy` (`src/hft_platform/strategies/cascade_bounce.py`) uses `_hold_ns` (configurable hold period, default 300s) and checks elapsed time on every tick/stats event (line 373: `if elapsed_ns >= self._hold_ns`). A 4-hour strategy would use the identical pattern with `_hold_ns = 4 * 3600 * 1e9`.

3. **Position tracking is persistent.** `PositionTracker` / `RustPositionTracker` tracks net position by (strategy, symbol). No timeout or expiry mechanism. Positions persist until explicitly closed by a counter-order. Cross-day continuity was hardened in the recent ops work (checkpoint + StartupPositionVerifier).

4. **OrderIntent supports TTL.** `OrderIntent.ttl_ns` (`contracts/strategy.py:57`) provides per-order expiry. A trend-following strategy can set long TTLs for passive limit entries.

**Potential concern**: A strategy holding for hours must handle session boundaries. TAIFEX has a 5-min break (13:30-13:35 for night session). CBS handles this via `TrackGate` (session phase filtering, `runner.py:204`). A trend-following strategy should respect the same mechanism.

**Verdict: No infrastructure gap.** Multi-hour holds work with existing StrategyRunner, same pattern as CBS.

### Feature Engine Integration

- vrr [21] is referenced as an input. **CAUTION**: `vrr_5_300_x1000` is NOT in the current FeatureEngine registry (`feature/registry.py` defines indices [0]-[20] only, last feature is `deep_depth_momentum_x1000` at [20]). If vrr was implemented in research but never registered, it must be formally added before this strategy can consume it.
- EMA/breakout signals are trivial to compute in-strategy or as new FE features. No infrastructure blocker.

### Position Limits and Margin

- TAIFEX TMFD6 position limit for retail: 100 lots (futures). Strategy holding 1 lot for hours is well within limits.
- Margin: TMFD6 initial margin ~41,000 NTD/lot. Multi-hour hold means overnight margin if crossing session close. Verify strategy auto-flattens before settlement.

### Implementation Timeline

- ~2-3 days. Straightforward BaseStrategy subclass following CBS pattern. Main work is regime detection logic and EMA parameters, not infrastructure.

### Verdict: APPROVE

No infrastructure gaps. Existing StrategyRunner + CBS-style hold pattern handles this natively.

---

## Candidate B: Execution Cost Reduction via Fill-Probability Optimization — CONDITIONAL APPROVE

### This IS my domain. Detailed assessment below.

### Researcher Question: Do we have enough historical fill data in ClickHouse?

**PARTIALLY.** Here is the actual data schema:

**`hft.orders`** (initial schema, `20260301_001_initial_schema.sql:39-52`):
```
order_id, strategy_id, symbol, side, price_scaled, qty, status, ingest_ts, latency_us
```

**`hft.fills`** (initial schema, `20260301_001_initial_schema.sql:245-261`):
```
ts_exchange, ts_local, client_order_id, broker_order_id, fill_id,
strategy_id, symbol, side, qty, price_scaled, fee_scaled, source
```

**`hft.fills` TCA extension** (`20260327_002_add_tca_columns_to_fills.sql`):
```
+ decision_price Int64, arrival_price Int64
```

**`hft.slippage_records`** (`20260325_001_add_slippage_records.sql`):
```
order_id, symbol, side, decision_mid, fill_price, slippage_ticks, slippage_ntd, latency_ns, ts
```

**`hft.shadow_orders`** (`20260320_001_add_shadow_orders.sql`):
```
ts_ns, strategy_id, symbol, side, price, qty, intent_type, intent_id
```

**What we HAVE for fill-probability modeling:**
- Fill prices, timestamps, sides, quantities
- Decision price and arrival price (slippage decomposition)
- Order submission latency
- Shadow orders (unfilled intents for negative examples)

**What we DO NOT HAVE:**
- **LOB state at order submission time** — This is the critical gap. Fill probability depends on queue position, book depth, spread, and imbalance at the moment of order placement. We record market data and orders separately but do NOT snapshot LOB features alongside each order.
- **Order lifecycle events** (partial fills, modifications, cancellations with timestamps) — `hft.orders` only has final `status`, not event log. `audit.orders_log` has `action` (NEW/AMEND/CANCEL) but no LOB context.
- **Queue position proxy** — We don't record how many contracts were ahead of our order at submission.

**Data sufficiency assessment:**
- For a **simple** fill-probability model (logistic regression on spread + imbalance at submission time): INSUFFICIENT. We need to join `hft.orders` with `hft.market_data` by timestamp to reconstruct LOB state. This is a cold-path query, feasible but requires careful timestamp alignment (~50-100 LOC diagnostic script).
- For a **production-grade** model (Lokin & Yu style, state-dependent): Need to add LOB feature snapshot to order records. This is a schema change + recorder modification (~100-150 LOC).
- **Volume**: We have ~58 days of data. With CBS and OpMM shadow, fill count depends on strategy activity. If OpMM shadow was running, we may have thousands of shadow orders as negative examples.

### R16 Passive Order Savings Validation

The researcher cites R16 finding that passive orders save 1.2 pts/trade. This is correct. Target of 1.5-2.0 pts/trade via systematic optimization is plausible IF we can model fill probability accurately. The 0.3-0.8 pts/trade improvement target is within reach.

### Latency Relevance

Ma et al. (2504.00846) on optimal execution with latency is DIRECTLY relevant. At 36ms P95 RTT, our order updates lag the market by ~4.5 ticks (at TMFD6 median tick interval 125ms, 36ms = 0.29 ticks... but during fast markets, tick intervals compress to 10-20ms, making 36ms = 2-4 ticks). Fill probability drops sharply when the market moves 1+ tick during our order flight time.

### Feature Engine Integration

- Fill-probability model inputs (spread, imbalance, OFI, vrr) are already in FE v2 (minus vrr, see Candidate A note).
- The model itself would NOT run in FeatureEngine. It would run in the OrderAdapter or a new ExecutionOptimizer module, consuming FE features at order submission time.
- Computational cost: O(1) per order (feature lookup + logistic/linear model evaluation). Negligible at order frequency (~10-100 orders/day).

### Implementation Timeline

- **Phase 1** (data collection, 1-2 weeks): Add LOB feature snapshot to order records. Accumulate 30+ days of enriched data.
- **Phase 2** (model training, 1 week): Offline logistic regression on fill probability. Cold-path research script.
- **Phase 3** (integration, 1-2 weeks): Wire model into OrderAdapter for dynamic limit price offset.
- **Total: 4-6 weeks** before meaningful production impact. This is NOT a 1-week project.

### Conditions

1. **Add LOB snapshot to order records** before meaningful fill-probability modeling can begin. Schema change required.
2. **Validate data sufficiency**: Query `SELECT count() FROM hft.fills` and `SELECT count() FROM hft.shadow_orders` to confirm we have enough examples.
3. **Start with simple heuristic**: Before ML model, implement spread-proportional limit offset as baseline (saves most of the 1.2 pts immediately, ~50 LOC). This can ship in 2-3 days.

### Verdict: CONDITIONAL APPROVE

High-value direction but timeline is understated. The simple heuristic (Phase 0) can deliver value quickly; the full model requires 4-6 weeks of data pipeline + training + integration.

---

## Candidate C: Calendar/Session Pattern Accumulation — APPROVE

### Signal vs Latency — PASS (trivially)

Event-driven orders at session boundaries with minutes of lead time. Latency is completely irrelevant.

### Researcher Question: Can we log gap-fade signals without code changes?

**MOSTLY YES, with one gap.**

**What we HAVE:**
- `hft.ohlcv_1m` materialized view computes 1-min OHLCV candles from market data. Previous close = `close_scaled` of last 1-min candle before session end.
- `hft.market_data` stores all ticks. First tick of the day gives opening price.
- ClickHouse queries can compute gap = (first_tick_price - prev_session_last_close) / prev_session_last_close without any code changes.

**What we DO NOT HAVE:**
- **No `previous_close` field anywhere in the runtime pipeline.** Grep for `previous_close` and `prev_close` across `src/hft_platform/` returns zero results. The runtime has no concept of "yesterday's closing price."
- **No daily OHLCV table** — only 1-min candles. Previous close must be derived as `MAX(close_scaled) WHERE bucket = last_minute_of_previous_session`. This is a ClickHouse query, not a code change.

**Automated logging approach (no code changes):**

1. Write a ClickHouse SQL query that computes daily gap for each symbol:
```sql
SELECT
    symbol,
    toDate(toDateTime(exch_ts / 1000000000)) AS trading_date,
    argMin(price_scaled, exch_ts) AS open_price,
    -- join with previous day's last price
    ...
```
2. Schedule via cron or the platform's report service (`src/hft_platform/reports/`). The report service already runs daily market analysis. Adding a gap-fade signal log is a configuration addition, not a code change.

**For live trading (requires code):**
- Strategy needs previous close at session open. Two options:
  - (a) Query ClickHouse at startup (~10 LOC in strategy `__init__`). Cold-path, acceptable.
  - (b) Add `previous_close` to `SymbolMetadata`. More robust but requires loader changes (~30 LOC).

### R17 Gap Fade Reference

R17 found Gap Fade (C1): +32 bps, 70.4% WR, p=0.060, N=27. Need 60+ observations. With ~58 days of data, we have at most 58 gap observations (one per trading day per symbol). This is borderline. Another 30+ days of accumulation would bring us to statistical significance.

### Implementation Timeline

- **Signal logging (no code)**: 1 day. Pure ClickHouse query + cron.
- **Strategy implementation**: 2-3 days. Simple BaseStrategy subclass: check gap at open, enter contrarian if gap > threshold, exit at fixed time or take-profit.
- **Statistical validation**: Needs 60+ days of data (currently ~58). May need to wait for accumulation OR use intraday sub-patterns that have more observations.

### Verdict: APPROVE

Trivial execution. Logging can start immediately with zero code changes. Strategy implementation is straightforward when data accumulates.

---

## Cross-Candidate Summary

| Candidate | Verdict | Timeline | Key Risk |
|-----------|---------|----------|----------|
| A: Regime Trend Following | APPROVE | 2-3 days | vrr not in FE registry (fixable) |
| B: Fill-Probability Optimization | CONDITIONAL APPROVE | 4-6 weeks (full) / 2-3 days (heuristic) | Missing LOB snapshot on orders |
| C: Calendar/Session Patterns | APPROVE | 1-3 days | N=58 gap observations (borderline) |

### Recommended Build Order

1. **Immediate (this week)**: Candidate C signal logging (ClickHouse query, 0 code). Candidate A strategy skeleton (2-3 days).
2. **Short-term (next 2 weeks)**: Candidate B Phase 0 (spread-proportional limit offset heuristic, 2-3 days). Add LOB snapshot to order schema for B Phase 1.
3. **Medium-term (4-6 weeks)**: Candidate B full fill-probability model after data accumulates.

### Config Drift: 0

No existing configs conflict with any proposed strategy. Clean slate for all three.

### vrr Registry Gap

Repeated from earlier analysis: `vrr_5_300_x1000` is NOT in `feature/registry.py` (21 features, indices [0]-[20]). Both Candidates A and B reference vrr. If needed, it must be formally registered. This is ~30 LOC registry + ~50 LOC kernel.
