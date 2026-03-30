# Round 17 -- OIDS Execution Review

**Date**: 2026-03-26
**Reviewer**: Claude (Execution reviewer agent)
**Scope**: Tradability assessment for OIDS (Options-Informed Directional Signal)

---

## 1. TXO Data Schema Assessment

### What We Actually Have

The ClickHouse schema (`migrations/clickhouse/20260301_001_initial_schema.sql`) stores ALL market data in a single generic `hft.market_data` table:

```sql
CREATE TABLE hft.market_data (
    symbol String,
    exchange String,
    type String,           -- 'Tick', 'BidAsk', 'Snapshot'
    exch_ts Int64,
    ingest_ts Int64,
    price_scaled Int64,    -- single price
    volume Int64,          -- single volume
    bids_price Array(Int64),
    bids_vol Array(Int64),
    asks_price Array(Int64),
    asks_vol Array(Int64),
    seq_no UInt64
)
```

The recorder mapper (`recorder/mapper.py:78`) maps TickEvent and BidAskEvent into this schema with no option-specific fields.

### What OIDS Needs

The survey states OIDS needs: **strike price, expiry date, put/call type, volume, trade price (to derive IV)**.

Assessment of each:

| Field | Available? | Source | Notes |
|-------|-----------|--------|-------|
| **Trade price** | YES | `price_scaled` column | Standard tick price |
| **Volume** | YES | `volume` column | Per-tick volume |
| **Bid/Ask** | YES | `bids_price`/`asks_price` arrays | L1-L5 LOB snapshots |
| **Strike price** | PARTIAL | Encoded in symbol code only | `TXO33500P6` → strike=33500. Must be parsed from symbol string. Not a separate column. |
| **Put/Call type** | PARTIAL | Encoded in symbol code only | `TXO33500P6` → P=put. `TXO34400D6` → D=call. Must be parsed from symbol string. |
| **Expiry date** | PARTIAL | Encoded in symbol code only | `TXO33500P6` → month code 6. Year not explicit. Must be derived from contract convention + `symbols.yaml` metadata. |
| **Underlying price** | NO | Not stored alongside options | Need TX/TMFD6 price at same timestamp for IV calculation. Requires cross-symbol timestamp join. |
| **Risk-free rate** | NO | Not in platform | Needed for Black-Scholes. Must be sourced externally or hardcoded. |
| **Days to expiry** | NO | Not computed | Must be derived from expiry date + current date. |

### Critical Finding

**Strike, put/call, and expiry are recoverable** from the symbol code (`TXO{strike}{P|D}{month_code}`) -- this is a parsing task, not a data gap. 42 TXO contracts are configured in `symbols.yaml` with `product_type: option` and tags including `atm`, `near_month`.

**Underlying price IS a data gap.** IV computation requires the TAIEX index or TX futures price at the same timestamp as each option tick. If TMFD6 data is in the same `market_data` table, a timestamp-based join is possible but expensive (33M options rows x nearest-match join). If TMFD6 data does not overlap temporally with TXO data, this is a harder problem.

### Verdict on Data: CONDITIONAL -- recoverable with engineering effort

The "33M rows" claim appears legitimate (42 TXO contracts x daily recording), but the data is in generic tick/bidask format. Option-specific fields must be derived, and underlying price must be joined from TMFD6 data. This is a **cold-path data engineering task**, not a hot-path concern.

---

## 2. IV Computation Pipeline

### Computation Requirements

Black-Scholes IV calculation requires per option tick:
1. Option price (have: `price_scaled`)
2. Strike price (derive: parse from symbol code)
3. Underlying price (gap: cross-symbol timestamp join)
4. Time to expiry (derive: from symbol month code + calendar)
5. Risk-free rate (gap: must source or approximate)
6. Dividend yield (gap: for TAIEX, can approximate as 0 for intraday)

### Cold Path vs Hot Path

**This is definitively a cold-path computation.** Rationale:

1. OIDS signal horizon is 5-60 minutes. Signal updates are needed at most once per option trade, not per TMFD6 tick.
2. IV computation involves Newton-Raphson iteration (5-15 iterations of exp/log). ~1-10 microseconds per option, but across 42 contracts x multiple ticks, this could be 1-50ms per batch update.
3. At 5-minute signal granularity, a 50ms computation every 5 minutes is negligible.

**No hot-path violation.** The IV pipeline would run as a background task (separate thread/process), publishing a summary signal (put/call volume imbalance, IV skew metric) that the strategy consumes as a slow-updating feature.

### Implementation Approach

1. **Offline prototype** (Stage 2): Query ClickHouse for TXO + TMFD6 data, compute IV offline in Python/numpy, calculate put/call volume imbalance, test IC.
2. **Live pipeline** (Stage 3+): Background worker subscribes to TXO ticks via FeatureEngine or dedicated service, computes rolling IV and volume imbalance, publishes to a shared signal store.

### Verdict on IV: PASS -- cold-path, no Constitution violation

---

## 3. Platform Integration

### Cross-Instrument Signal Architecture

This is the same architectural challenge as CSLL (Round 17 Stage 1, REJECTED). OIDS requires:

1. **Signal source**: TXO options data (42 symbols)
2. **Trading target**: TMFD6 (1 symbol)
3. **Signal flow**: TXO ticks → IV + volume computation → directional signal → TMFD6 strategy

Current `StrategyRunner` (`strategy/runner.py:496`) dispatches events per-symbol via `process_event()`. The event's `symbol` attribute determines routing. A strategy registered for TMFD6 will NOT receive TXO events.

### Key Difference from CSLL

Unlike CSLL (which needed real-time cross-symbol tick alignment at <550ms latency), OIDS operates at **5-60 minute horizons**. This means:

1. The TXO signal can be computed **asynchronously** in a background worker, completely decoupled from the TMFD6 hot path.
2. The strategy only needs to read a **pre-computed signal value** (e.g., `oids_put_call_imbalance_x1000`), not process raw TXO events.
3. This is architecturally equivalent to adding an external feature that updates every few minutes -- similar to how one might consume an external API signal.

### Integration Options

**(A) FeatureEngine external signal injection** (Recommended):
- Add a `set_external_feature(symbol, feature_id, value)` method to FeatureEngine.
- Background OIDS worker computes signal, injects into FeatureEngine for TMFD6.
- Strategy consumes via standard `on_features()` / `FeatureUpdateEvent`.
- **Blast radius**: Small. New method on FeatureEngine + background worker. No changes to StrategyRunner, RiskEngine, or OrderAdapter.

**(B) Standalone signal store**:
- Dedicated OIDS service writes to a shared dict/Redis.
- Strategy reads from the store in `on_stats()` before entry decision.
- **Blast radius**: Medium. New service + new dependency (Redis or shared memory).

**(C) Multi-symbol strategy subscription**:
- Register strategy for both TMFD6 and all 42 TXO symbols.
- Strategy internally routes TXO events to signal computation, TMFD6 events to trading logic.
- **Blast radius**: Large. 42 additional subscriptions flooding StrategyRunner. Performance concern.

**Recommendation: Option (A).** External signal injection into FeatureEngine is the cleanest path. It preserves the per-symbol dispatch model and avoids hot-path contamination.

### Verdict on Integration: CONDITIONAL APPROVE with Option (A) architecture

---

## 4. Implementation Effort

### Phase 1: Offline Prototype (Stage 2) -- Effort: M (Medium)

| Component | LOC | Notes |
|-----------|-----|-------|
| TXO symbol parser (strike, P/C, expiry from code) | ~50 | Regex on `TXO{strike}{P|D}{month}` |
| ClickHouse query for TXO + TMFD6 aligned data | ~80 | SQL with timestamp-based ASOF JOIN |
| IV computation (Black-Scholes, Newton-Raphson) | ~100 | scipy or manual implementation |
| Put/call volume imbalance metric | ~50 | Aggregation over IV-weighted volumes |
| IC test against TMFD6 returns at 5/15/30/60 min | ~80 | Standard IC/correlation analysis |
| **Total Stage 2** | **~360** | **1-2 sessions** |

### Phase 2: Live Pipeline (Stage 3+) -- Effort: L (Large)

| Component | LOC | Notes |
|-----------|-----|-------|
| Background OIDS worker (async, subscribes to TXO ticks) | ~200 | New service module |
| FeatureEngine `set_external_feature()` method | ~50 | New method + registry support |
| Feature registry v3 with `oids_signal_x1000` | ~20 | Registry bump |
| CBS/strategy gating logic | ~30 | Similar to EGVT gate |
| Tests | ~200 | Worker + integration + feature tests |
| **Total Stage 3** | **~500** | **2-3 sessions** |

### End-to-End: M-L (Medium-Large), 3-5 sessions total

---

## 5. Latency Budget

### Signal Update Frequency

TXO options trade less frequently than TMFD6. With 42 contracts, aggregate TXO tick rate might be 5-20 ticks/sec across all strikes. The put/call imbalance signal is aggregated over 5-60 minute windows.

**Signal update cadence**: Every 1-5 minutes (batch recomputation) or on each TXO trade (incremental update).

**Pipeline timing**:
- TXO tick → background worker: ~0 (direct subscription, no hot-path involvement)
- IV computation per tick: ~1-10 microseconds
- Signal aggregation: ~100 microseconds
- Feature injection to FeatureEngine: ~1 microsecond
- Strategy reads on next TMFD6 event: standard pipeline latency (250 microseconds)

**Total signal delay**: Well under 1 second from TXO trade to TMFD6 strategy consumption. For a 5-60 minute signal horizon, this is negligible.

### Verdict on Latency: PASS -- no latency concern at any timescale

---

## Overall Verdict: **CONDITIONAL APPROVE**

### Conditions

1. **Must validate data quality first** (Stage 2 gate): Before any live pipeline work, run the offline prototype to confirm:
   - TXO data in ClickHouse has sufficient temporal overlap with TMFD6
   - IV can be reliably computed from available fields (price + parsed strike + joined underlying)
   - Put/call volume imbalance has IC > 0.02 against TMFD6 returns at target horizons
2. **Architecture must use Option (A)**: External signal injection into FeatureEngine, NOT multi-symbol strategy subscription.
3. **Background worker must be non-blocking**: OIDS computation must never touch the TMFD6 hot path. Separate thread/process with feature injection only.
4. **Risk-free rate approximation must be documented**: Using a constant (e.g., Taiwan 1-year treasury rate) is acceptable for intraday IV, but must be explicitly stated in research artifacts.

### Key Risk: Data Quality Unknown

The biggest uncertainty is not implementation complexity -- it is whether the 33M TXO rows contain sufficient data quality for IV computation. Specifically:
- Are the option ticks liquid enough (bid-ask spread, trade frequency) for meaningful IV?
- Is temporal overlap with TMFD6 data continuous or gapped?
- Are all 42 configured strikes actually traded, or are most illiquid?

**Recommendation**: Run the offline Stage 2 prototype BEFORE committing to any live pipeline work. If IC < 0.02 or data quality is poor, OIDS should be dropped immediately.

### Comparison to EGVT

| Dimension | EGVT | OIDS |
|-----------|------|------|
| Data ready? | YES (TickEvent) | CONDITIONAL (needs offline validation) |
| Implementation effort | M (300 LOC) | M-L (860 LOC total) |
| Architecture change | Small (process_tick) | Medium (external feature injection) |
| Signal novelty | Orthogonal to CBS (timing gate) | Fundamentally new (cross-instrument) |
| Edge potential | 2-4 bps (paper) | Unknown on TAIFEX |
| Risk | Undersampled matrix | Data quality unknown |

EGVT remains the safer primary candidate. OIDS has higher potential upside but higher uncertainty. **Recommend OIDS as Stage 2 offline validation in parallel with EGVT prototype**, with a clear kill gate: IC < 0.02 on TMFD6 = drop.
