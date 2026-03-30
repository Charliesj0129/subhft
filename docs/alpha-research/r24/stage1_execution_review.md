# R24 Stage 1: Execution Review

**Reviewer**: Execution Review Agent
**Date**: 2026-03-29
**Target**: `docs/alpha-research/r24/stage1_literature_survey.md`

---

## Direction A: Execution Quality Optimization via Fill Probability Modeling — CONDITIONAL APPROVE

### Latency Assessment

- **Broker RTT**: Shioaji P95 submit = 36ms, cancel = 47ms (source: `config/research/latency_profiles.yaml`, measured 2026-03-04). The fill probability model decision occurs BEFORE order submission, so broker RTT is not the binding constraint — local inference latency is.
- **Kill gate claim (100us)**: The report claims 100us latency budget for logistic regression on 27 features. This is realistic for a pre-fit sklearn logistic regression (`predict_proba` on a 27-element vector). NumPy dot product + sigmoid on 27 floats is ~1-5us. Even a shallow NN (27->16->1) would be ~10-50us. **PASS**.
- **Local decision pipeline**: 250us total pipeline latency budget per the latency profile. Adding 5-50us for fill probability inference is within budget (~2-20% overhead). **PASS**.
- **Signal horizon**: Per-order decision, not time-series. Compatible with execution latency since the model informs the limit-vs-market choice BEFORE the order enters the broker path.

### Feature Mapping

- All 27 v3 features confirmed in `src/hft_platform/feature/registry.py`:
  - `toxicity_ema50_x1000` = index [21] in `lob_shared_v2`/`v3` — **CONFIRMED** (registry.py:186)
  - `ret_autocov_5s_x1e6` = index [17] — **CONFIRMED** (registry.py:163)
  - `tob_survival_ms` = index [18] — **CONFIRMED** (registry.py:167)
  - `spread_ema300s` = index [26] — **CONFIRMED** (registry.py:219)
- **API drift**: Report mentions `FeatureEngine.get_latest()` — this method DOES NOT EXIST. Actual API is `get_feature_tuple(symbol)`, `get_feature(symbol, feature_id)`, `get_feature_by_index(symbol, idx)`, or `get_feature_view(symbol)` (engine.py:307-328). The ~30 LOC wiring estimate should reference these actual methods.

### Infrastructure Feasibility

- **ExecutionOptimizer baseline** (`src/hft_platform/execution/execution_optimizer.py`, 216 LOC): Simple stateless heuristic using `spread_pts`, `near_depth`, `opp_depth`, `imbalance_ppm`. No feature engine integration today. The report's characterization is accurate.
- **Feature exposure to execution layer**: Currently `ExecutionOptimizer.decide()` takes raw LOB values (spread, depth, imbalance) — no FeatureEngine dependency. Wiring `get_feature_tuple()` into the execution path requires modifying the call site (likely in `cascade_bounce.py:139` or `order/adapter.py`). 30 LOC is reasonable.
- **`hft.execution_decisions` table**: Does NOT exist. Only referenced in the report. Migration + recorder change at ~50 LOC is a reasonable estimate given existing migration patterns (see `20260325_001_add_slippage_records.sql` as template).
- **Backtest replay framework**: ~200 LOC estimate. No existing execution replay infrastructure. This is the riskiest estimate — proper replay with LOB state reconstruction could easily exceed 200 LOC. **Flagged as optimistic but not blocking**.

### Config Consistency

- **COST DRIFT DETECTED**: Report claims "TMFD6 RT cost of 3.92 pts (1.19 bps)". Verified fee structure: TMFD6 RT cost = **40 NTD = 4.0 pts = 1.33 bps**. The 3.92 pts figure is from the older TMFD6 OpMM research which used a different fee breakdown. The current authoritative number is **4.0 pts RT**.
  - Impact: "even 0.5 bps improvement is material (42% cost reduction)" — at 1.33 bps, 0.5 bps = 37.6% reduction, not 42%. Directionally correct but numbers need updating.
  - **Severity**: LOW. Does not change the approve/reject decision. Cost is HIGHER than reported, which makes execution optimization MORE valuable, not less.

### Verdict: CONDITIONAL APPROVE

**Conditions**:
1. Fix cost assumption: 4.0 pts / 1.33 bps (not 3.92 / 1.19)
2. Use actual FeatureEngine API names (`get_feature_tuple`, not `get_latest`)
3. Flag backtest replay at 200-400 LOC (not 200)

---

## Direction B: Cross-Instrument Options Flow Pipeline — CONDITIONAL APPROVE

### Latency Assessment

- **Signal horizon 30s-300s**: Well above broker RTT (36-47ms). No latency concern.
- **Options feature computation latency**: Put-call ratio and options OFI are simple aggregation operations. No latency risk for MF signals.

### Feature Mapping

- Not directly dependent on existing FeatureEngine indices (new module). **N/A**.

### Infrastructure Feasibility

- **TXO subscription**: The report claims "Shioaji/Fubon API already supports TXO subscription". Verified: NO TXO-specific subscription code exists in `src/hft_platform/feed_adapter/`. Zero grep matches for "TXO", "txo", or "option.*subscribe" in the feed_adapter directory. The broker SDK likely supports options subscriptions, but NO platform code handles it today. The report UNDERSTATES the effort.
- **InstrumentRegistry**: Fully supports options metadata (`InstrumentType.OPTION`, `OptionRight`, `strike_scaled`, `expiry`, `get_options_chain()`) — `src/hft_platform/core/instrument_registry.py`. **CONFIRMED**.
- **ClickHouse schema**: Migration `20260330_001_add_instrument_columns.sql` adds `instrument_type`, `underlying`, `strike_scaled`, `option_right`, `expiry` to `hft.market_data`, `hft.orders`, `hft.fills`. **CONFIRMED — schema ready**.
- **Normalizer**: Report claims normalizer handles multi-instrument via `instrument_type` field. This was added in recent commits (`a0b2793d`, `009b47b1`). **CONFIRMED**.
- **Options feature module (~300-500 LOC)**: Estimate is reasonable for a new module computing put-call ratio + options OFI + implied vol proxy.
- **Cross-instrument bus wiring (~100 LOC)**: Reasonable but may be optimistic. Current `RingBufferBus` is symbol-keyed. Routing options features to futures strategies requires either a new bus channel or a multi-symbol aggregation layer. Could be 100-200 LOC.
- **R17 data quality warning**: Report correctly identifies the TXO data quality risk (99.7% quotes). This is the dominant risk. Kill gate at <100 trades/day is appropriate.

### Config Consistency

- No config drift for Direction B. New config items (TXO symbols) are additive.

### Data Pipeline Reality Check

- **Feed adapter gap**: The report says TXO subscription needs "~5 LOC in symbols.yaml". Reality: needs subscription handler code in `quote_runtime.py` for the active broker, plus normalizer wiring for options tick format differences. This could be 50-100 LOC in feed_adapter, not 5.
- **4-week data accumulation**: Correctly identified as lead time. No shortcut.

### Verdict: CONDITIONAL APPROVE

**Conditions**:
1. Correct the feed_adapter LOC estimate: add 50-100 LOC for subscription handling (not just 5 LOC config)
2. Data quality gate MUST fire first: subscribe to TXO in current deployment, count trade ticks for 5 days before committing to feature engineering
3. Cross-instrument bus wiring estimate: 100-200 LOC (not 100)

---

## Direction C: Adaptive Execution Timing via Regime Detection — CONDITIONAL APPROVE

### Latency Assessment

- **Regime transition detection**: Regime classifier combines existing features into discrete states. For threshold-based classifier: O(1), ~1us. For logistic regression: same as Direction A (~5-50us). **PASS**.
- **Execution gating adds latency in decision path**: Minimal — one additional boolean check before order submission. ~0 overhead. **PASS**.

### Feature Mapping

- `toxicity_ema50_x1000` [21] — **CONFIRMED** (registry.py:186, engine.py:133-135, `source_kind="tick"`). Computed via `on_tick()`.
- `ret_autocov_5s_x1e6` [17] — **CONFIRMED** (registry.py:163, warmup=42 events).
- `tob_survival_ms` [18] — **CONFIRMED** (registry.py:167, warmup=2 events).
- `spread_ema300s` [26] — **CONFIRMED** (registry.py:219, warmup=2400 events. Note: 2400 events at 125ms cadence = 5 minutes warmup).
- **BurstDetector** — **CONFIRMED** (`src/hft_platform/feature/burst_detector.py`, 236 LOC). Standalone module, not directly wired into FeatureEngine output tuple. Integration point is separate from feature indices.
- **VRR DOES NOT EXIST**: Report references "VRR (variance ratio) feature" as existing. Per code verification: `vrr_5_300_x1000` was **NEVER registered** in the FeatureEngine. It was dead code from R22. Toxicity took slot [21] in R23. No VRR feature exists at any index. **CONFIG DRIFT**.
  - Impact: Direction C's regime classifier loses one of its claimed inputs. The remaining 4 features (toxicity, ret_autocov, tob_survival, spread_ema300s) + BurstDetector are still sufficient for a regime classifier, but the report's design should be corrected.

### Infrastructure Feasibility

- **Regime classifier (~150 LOC)**: Reasonable. Threshold-based classifier on 4-5 features is simple.
- **Integration into ExecutionOptimizer (~50 LOC)**: Reasonable. Add a `regime_gate` check in `ExecutionOptimizer.decide()`.
- **Backtest replay (~100 LOC)**: Same concern as Direction A — replay with proper feature reconstruction could exceed estimate.
- **Total ~300 LOC**: With VRR removed, likely closer to 250 LOC (one fewer feature to handle). Still the lowest-cost direction.

### Config Consistency

- **VRR drift**: Report claims VRR exists as an input. It does not. See above.
- **BurstDetector integration**: BurstDetector is a standalone class, not a FeatureEngine index. Report should clarify that BurstDetector state is accessed via its own API (`is_burst`, `tick_rate`), not via feature indices.

### Verdict: CONDITIONAL APPROVE

**Conditions**:
1. **Remove VRR from design** — it does not exist. Replace with explicit acknowledgment that regime classifier uses 4 features + BurstDetector state.
2. Clarify BurstDetector integration point (standalone API, not feature index).
3. Fix backtest replay estimate to 100-200 LOC.

---

## Overall Verdict: CONDITIONAL APPROVE

All 3 directions are conditionally approved. No direction has a fatal infrastructure blocker. The priority ordering C > A > B is sound from an execution feasibility perspective.

## Config Drift Items

| Item | Report Claim | Actual | Severity | Blocking? |
|------|-------------|--------|----------|-----------|
| TMFD6 RT cost | 3.92 pts / 1.19 bps | **4.0 pts / 1.33 bps** | LOW | No (makes optimization more valuable) |
| FeatureEngine API | `get_latest()` | `get_feature_tuple()` / `get_feature()` / `get_feature_by_index()` / `get_feature_view()` | LOW | No (trivial rename in design) |
| VRR feature | Exists as input for Direction C | **DOES NOT EXIST** (never registered, dead code from R22) | **MEDIUM** | No (4 features + BurstDetector still sufficient) |
| TXO subscription | "~5 LOC in symbols.yaml" | Need 50-100 LOC in feed_adapter subscription handler + config | LOW | No (increases effort estimate) |
| Backtest replay | 200 LOC (A), 100 LOC (C) | 200-400 LOC (A), 100-200 LOC (C) | LOW | No (effort underestimate) |

**Total config drift items: 5** (0 HIGH severity, 1 MEDIUM, 4 LOW).

Per review protocol, config drift > 0 requires conditional approval with fixes identified. All fixes are enumerated above. None are blocking — all are correctable in the Stage 2 design document.

## Recommendations

1. **Immediate**: Fix cost numbers and API names in the report before Stage 2.
2. **Direction C first**: Confirmed as lowest risk and lowest effort. Start immediately.
3. **Direction A**: Start infrastructure (execution_decisions table, feature wiring) in parallel with C validation.
4. **Direction B**: Subscribe to TXO now for data accumulation. Gate the 500 LOC feature engineering behind 5-day trade tick count validation.
