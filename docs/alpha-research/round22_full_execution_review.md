# Round 22: Full Execution Review — Tradability and Implementation Feasibility

**Date**: 2026-03-28
**Reviewer**: Execution Reviewer (Alpha Research Team)
**Scope**: All Tier 0/1/2 directions from Round 22 expanded surveys
**Platform**: `hft-platform` — Python 3.12 + Rust (PyO3) + ClickHouse + Prometheus
**Broker**: Shioaji (P95 RTT: submit 36ms, cancel 47ms)
**Tick interval**: TXFD6 median 125ms, TMFD6 ~300ms

---

## Part 1: Per-Direction Feasibility Assessment

### Tier 0 — Immediate Execution

---

#### T0.1: Instantaneous Volatility Invariant

**Proposed**: `sigma = spread * sqrt(V_traded / depth) * P(spread/tick)` (Danyliv 2019)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | All fields available. `spread_scaled` (index 3), `bid_depth`/`ask_depth` (indices 4-5), `volume` from `TickEvent.volume`. `P(spread/tick)` requires tick-size config constant. |
| **Pipeline integration** | FeatureEngine. Computed inside `_compute_values()` after v2 features. Requires both `LOBStatsEvent` (depth/spread) and `TickEvent` (volume). **PROBLEM**: FeatureEngine currently consumes only `LOBStatsEvent` via `process_lob_update()`. TickEvent volume is not passed in. |
| **Hot-path safety** | Pure arithmetic: 1 multiply, 1 sqrt, 1 divide. `math.isqrt()` for integer path or bounded float. No allocation. PASS. |
| **Latency budget** | ~50ns compute. Negligible vs 125ms tick budget. PASS. |
| **Feature slot** | Slot [22] in `lob_shared_v3`. Requires registry bump. |
| **State** | Stateless per-tick (volume is instantaneous). No rolling state needed for the raw formula. EMA smoothing optional (adds 1 float state). |
| **Rust portability** | Trivial. Pure arithmetic. |
| **Config** | `tick_size` per symbol (already in `symbols.yaml`). No new config needed. |

**Infrastructure gap**: FeatureEngine needs access to `TickEvent.volume` alongside `LOBStatsEvent`. Currently the pipeline calls `process_lob_update(event, stats)` where `event` is the raw `BidAskEvent`. The `TickEvent` is processed separately. Two options:
1. Pass latest `TickEvent.volume` as an accumulator parameter to `process_lob_update()`.
2. Add a `process_tick()` method to FeatureEngine that caches latest volume per symbol.

**Verdict**: **CONDITIONAL APPROVE** — blocked by TickEvent volume access in FeatureEngine. Fix: ~20 LOC to add volume accumulator. Total LOC: ~40.

---

#### T0.2: Execution Optimizer (Limit/Market Switch)

**Proposed**: Fill probability model f(Q_near, Q_opp, imbalance) -> limit vs market decision (Albers 2025, R^2=0.946)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | All features available: `l1_bid_qty` (index 8), `l1_ask_qty` (index 9), `l1_imbalance_ppm` (index 10), `spread_scaled` (index 3). Recent trade direction derivable from `TickEvent.price` diffs. |
| **Pipeline integration** | Sits BELOW strategy layer, in `execution/` package. Integrates with `OrderAdapter` as a pre-dispatch decision. Already partially implemented: `src/hft_platform/execution/imbalance_timer.py` exists with threshold-based imbalance waiting. |
| **Hot-path safety** | Decision is O(1) comparison of 3-5 integer features. No allocation. Fire-and-forget policy (no dynamic cancel/reinsert). PASS. |
| **Latency budget** | Decision computed once per CBS signal (~7/day). Not tick-frequency. Budget irrelevant. |
| **Feature slot** | No new FeatureEngine slot needed. Uses existing features. |
| **State** | Minimal: current LOB state at decision time + timeout timer. `ImbalanceTimer` already manages this. |
| **Rust portability** | Not needed. Called 7x/day. |
| **Config** | `spread_threshold_pts: int`, `fill_score_threshold: float`, `limit_timeout_s: int`, `vol_threshold: float`. Under `strategies.CBS.execution` in YAML. |

**Infrastructure gap**: `ImbalanceTimer` exists but implements only imbalance-based delay. Needs extension to full limit/market decision framework with:
- Spread check (spread >= 2 ticks)
- Fill score computation (Q_opp / Q_near)
- Volatility gate (from VRR or instantaneous vol)
- Timeout + fallback to market order

**Existing code**: `src/hft_platform/execution/imbalance_timer.py` (80 LOC). Extend, don't rewrite.

**Verdict**: **APPROVE** — zero infrastructure blockers. Extend `ImbalanceTimer` to `ExecutionOptimizer`. ~150 LOC total (70 new).

---

#### T0.3: HAR-Style Multi-Window Aggregation

**Proposed**: 3-window EMA (5s/30s/300s) on existing 21 features (Corsi 2009)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | All 22 features from `lob_shared_v2` are available. No new data. |
| **Pipeline integration** | Two options: (A) Inside FeatureEngine as additional computed features, or (B) As a separate `AggregationEngine` downstream. Option B is cleaner — FeatureEngine tuple is already 22 slots wide. Adding 63+ aggregated features would bloat the tuple beyond practical limits. |
| **Hot-path safety** | O(1) per tick per feature per window. 22 features x 3 windows = 66 EMA updates. Each is 1 multiply + 1 add = ~130 FP ops total. ~200ns. PASS. |
| **Latency budget** | 200ns << 125ms. PASS. |
| **Feature slot** | NOT in FeatureEngine tuple. Separate data structure. `AggregationEngine` emits its own event or provides accessor API. |
| **State** | 22 x 3 = 66 float EMA states per symbol. ~528 bytes/symbol. With 10 symbols: 5.3 KB. Negligible. |
| **Rust portability** | Excellent. Array of EMA states, vectorizable. |
| **Config** | `aggregation.windows: [5, 30, 300]` (seconds). `aggregation.enabled: bool`. |

**Infrastructure gap**: Needs a new `AggregationEngine` class that subscribes to `FeatureUpdateEvent` and maintains multi-window EMAs. This is a new module but follows existing patterns (FeatureEngine consumes LOBStatsEvent, AggregationEngine consumes FeatureUpdateEvent).

**Design decision**: Should aggregated features flow to strategies via:
(A) Extended `FeatureUpdateEvent` with aggregation fields — breaks existing consumers.
(B) Separate `AggregatedFeatureEvent` — clean but adds bus traffic.
(C) Pull model: strategy calls `agg_engine.get(symbol, feature_id, window)` — simplest.

Recommend (C) for prototype, migrate to (B) for production.

**Verdict**: **APPROVE** — no blockers. New `AggregationEngine` module. ~100 LOC.

---

### Tier 1 — Low-Cost Exploration

---

#### T1.1: Trade Classification (EMO Algorithm)

**Proposed**: at-bid/at-ask + tick rule fallback, 85-90% accuracy (Jurkatis 2020)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | `TickEvent.price` (trade price), `BidAskEvent.bids[0][0]` / `BidAskEvent.asks[0][0]` (best bid/ask at trade time). Both available. **CRITICAL**: Classification requires knowing the prevailing bid/ask AT the time of each trade. Currently, `TickEvent` and `BidAskEvent` are processed by separate callbacks. Must cache latest bid/ask per symbol in normalizer. |
| **Pipeline integration** | Normalizer stage (`src/hft_platform/feed_adapter/normalizer.py`). Add `_last_best_bid` / `_last_best_ask` per-symbol cache. On each `TickEvent`, compare `price` to cached bid/ask. Output: add `trade_direction: int` field to `TickEvent` (+1=buy, -1=sell, 0=unknown). |
| **Hot-path safety** | O(1) integer comparison. `mid_x2 = best_bid + best_ask; trade_x2 = price * 2; compare.` No float, no allocation. PASS. |
| **Latency budget** | ~10ns per classification. Negligible. PASS. |
| **Feature slot** | Not a FeatureEngine feature per se. It's a `TickEvent` field enrichment. Downstream features (signed OFI, Hawkes) consume it. |
| **State** | 2 ints per symbol: `last_best_bid`, `last_best_ask`, plus 1 int `prev_direction` for tick-rule fallback. ~24 bytes/symbol. |
| **Rust portability** | Trivial. Could be added to `normalize_tick_v2` Rust path. |
| **Config** | `HFT_TRADE_CLASSIFICATION_ENABLED=1` env var. Algorithm selection: `HFT_TRADE_CLASSIFIER=emo` (default). |

**Infrastructure gap**: `TickEvent` dataclass needs a new `trade_direction` field (default 0). This is a schema change affecting:
- `TickEvent` in `events.py` — add field
- `normalize_tick` paths — populate field
- ClickHouse `tick` table — add `trade_direction Int8` column
- `map_tick_record` in Rust — include new field
- Recorder pipeline — pass through

This is the single highest-leverage infrastructure investment. ~100 LOC across 4-5 files.

**Verdict**: **APPROVE** — high-priority infrastructure. Unlocks T1.2, T1.3, T1.4, T2.1, and future signed OFI/Hawkes work.

---

#### T1.2: Hawkes Branching Ratio (Regime Indicator)

**Proposed**: Estimate endogeneity from tick timestamps in rolling windows (Hardiman 2013)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | Only tick timestamps needed. Available from `TickEvent.meta.source_ts` (exchange timestamp). |
| **Pipeline integration** | NOT in FeatureEngine hot path. This is a slow regime indicator updated every 5 minutes. Compute in a background task or in the strategy layer. CBS already has a `_price_buf` deque for detection windows — similar pattern for timestamp buffer. |
| **Hot-path safety** | Not on hot path. 5-min calibration runs offline (batch fit on ~2400 timestamps). Even naive MLE fit of exponential Hawkes is O(N^2) with N=2400 -> ~6M ops. At ~1 GHz effective = ~6ms. Acceptable for 5-min cadence. |
| **Latency budget** | N/A (off hot-path). |
| **Feature slot** | Not in FeatureEngine tuple. Strategy-level feature or separate regime service. |
| **State** | Ring buffer of ~2400 timestamps (5 min at 8 ticks/s). ~19 KB per symbol. Acceptable. |
| **Rust portability** | Excellent candidate. Hawkes MLE is CPU-bound numeric optimization. `LobFeatureKernelV1` pattern. |
| **Config** | `hawkes.window_s: 300`, `hawkes.update_interval_s: 60`, `hawkes.kernel: exponential`. |

**Infrastructure gap**: None for prototype. Needs a `HawkesEstimator` class that accumulates timestamps and periodically refits. ~50 LOC Python prototype, ~200 LOC for production Rust kernel.

**RISK**: Estimation noise with only ~2400 samples. Must validate that branching ratio variability across windows is genuine (not fitting noise). Kill gate: std(n) < 0.05 across windows = signal is noise.

**Verdict**: **CONDITIONAL APPROVE** — pending Gate Zero validation of branching ratio variability on TXFD6/TMFD6. Python prototype first.

---

#### T1.3: Symmetric/Antisymmetric OFI Decomposition

**Proposed**: `sym = delta_bid_vol + delta_ask_vol`, `antisym = delta_bid_vol - delta_ask_vol` (Elomari-Kessab 2024)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | `delta_bid_vol` and `delta_ask_vol` are already computed internally by FeatureEngine for OFI L1 (`_compute_ofi_l1_raw`). Currently combined into single OFI value. Decomposition is trivial. |
| **Pipeline integration** | FeatureEngine `_compute_values()`. Modify `_compute_ofi_l1_raw` to return both components, then compute sym/antisym. |
| **Hot-path safety** | 2 additions. O(1). PASS. |
| **Latency budget** | ~5ns. PASS. |
| **Feature slot** | Slots [22] and [23] in `lob_shared_v3` (or v2 extension). |
| **State** | Reuses existing `prev_l1_bid_qty`, `prev_l1_ask_qty` from `_LobKernelState`. No new state. |
| **Rust portability** | Already in Rust (`LobFeatureKernelV1`). Trivial extension. |
| **Config** | None needed. Always compute if schema version >= 3. |

**Infrastructure gap**: NONE. This is a decomposition of an existing computation. Literally 2 lines of arithmetic change in `_compute_ofi_l1_raw` return path plus 2 new `FeatureSpec` entries.

**Verdict**: **APPROVE** — zero cost, zero risk. ~30 LOC total. Should be done first.

---

#### T1.4: Trade Sign Autocorrelation

**Proposed**: Rolling autocorrelation of classified trade signs; drop = large trader entry (Primicerio 2018)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | Requires trade classification (T1.1). Blocked until `trade_direction` field exists on `TickEvent`. |
| **Pipeline integration** | FeatureEngine or separate feature module. Rolling autocorrelation over N=100 classified trades. |
| **Hot-path safety** | O(1) per tick with ring buffer + running sums. Standard online autocorrelation formula. No allocation if ring buffer pre-allocated. PASS. |
| **Latency budget** | ~50ns per update. PASS. |
| **Feature slot** | Slot [24] in v3. |
| **State** | Ring buffer of 100 trade signs (100 bytes) + running sums (3 floats). ~120 bytes/symbol. |
| **Rust portability** | Good. Ring buffer + arithmetic. |

**Verdict**: **CONDITIONAL APPROVE** — blocked by T1.1 (trade classification). ~30 LOC after T1.1 exists.

---

#### T1.5: Tick-Rate Volatility Estimator

**Proposed**: `tick_count_ratio = tick_count(30s) / tick_count(300s)` as volatility proxy (Lee 2019)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | Only tick timestamps needed. Available. |
| **Pipeline integration** | Can be computed in FeatureEngine or AggregationEngine. Needs tick-count accumulators at 30s and 300s windows. |
| **Hot-path safety** | O(1) per tick: increment counter, check window boundary, divide. PASS. |
| **Latency budget** | ~10ns. PASS. |
| **Feature slot** | Slot [22] or [23] in v3 (if sym/antisym OFI doesn't take these). |
| **State** | 2 counters + 2 window-start timestamps per symbol. 32 bytes. |
| **Rust portability** | Trivial. |
| **Config** | `tick_rate.fast_window_s: 30`, `tick_rate.slow_window_s: 300`. |

**Infrastructure gap**: Window-based counters need tick-time-aligned reset. Two approaches:
(A) True sliding window with ring buffer (accurate but ~2400 entries).
(B) Exponential decay approximation (O(1) state, approximate).

Recommend (B) for hot-path, (A) for research validation.

**RISK**: Likely redundant with VRR (slot [21], `vrr_5_300_x1000`). Must measure correlation. Kill gate: rho > 0.9 with VRR.

**Verdict**: **CONDITIONAL APPROVE** — pending orthogonality check vs VRR. ~20 LOC.

---

#### T1.6: Cancellation Rate Asymmetry

**Proposed**: Infer bid/ask cancellation rate from depth decreases in snapshot diffs (Anantha 2025)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | From L1 snapshot diffs: when `l1_bid_qty` decreases AND `best_bid` unchanged = cancellation (or partial fill). Similarly for ask. Already have `prev_l1_bid_qty`, `prev_l1_ask_qty` in `_LobKernelState`. |
| **Pipeline integration** | FeatureEngine `_compute_values()`. After OFI computation, separate depth decreases into bid-side and ask-side cancellation proxies. |
| **Hot-path safety** | O(1) comparisons and subtractions. PASS. |
| **Feature slot** | Slot [24] or [25]: `cancel_rate_asym_ppm`. |
| **State** | EMA of bid-side and ask-side cancellation rates. 2 floats per symbol. |
| **Rust portability** | Trivial. |

**Infrastructure gap**: NONE. All data is available. The approximation (depth decrease at same price = cancellation) is imperfect but validated in the survey literature.

**RISK**: On TMFD6 with median depth = 1 lot, a depth decrease from 1->0 is always total depletion. This makes cancellation rate == depletion rate == execution rate. The signal may not distinguish cancellations from fills. Must validate.

**Verdict**: **CONDITIONAL APPROVE** — feasible but signal quality uncertain on thin books. ~40 LOC.

---

#### T1.7: Log-GOFI Stationarization

**Proposed**: `log_ofi = log(1 + |OFI|) * sign(OFI)` (Su 2021)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | Uses existing `ofi_l1_ema8` (index 13). |
| **Pipeline integration** | FeatureEngine or AggregationEngine. One-line transform. |
| **Hot-path safety** | One `log` call per tick. `math.log` is ~20ns. PASS. |
| **Feature slot** | Could replace `ofi_l1_ema8` or be an additional slot. |
| **State** | Stateless transform. |

**Verdict**: **APPROVE** — nearly zero cost. ~5 LOC. Test as offline IC comparison first before adding to live pipeline.

---

### Tier 2 — Medium-Cost Prototypes

---

#### T2.1: Metaorder Detection

**Proposed**: Reconstruct institutional order splitting from classified trade sequences (Maitrier/Bouchaud 2025)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | Requires classified trades (T1.1) with confidence weighting. |
| **Pipeline integration** | Offline research first. NOT hot-path. Batch analysis on ClickHouse historical data. |
| **State** | Complex: running segmentation of trade sequences by direction persistence, size clustering. Hundreds of bytes per symbol. |
| **Rust portability** | Medium complexity. Pattern matching on sequences. |

**RISK**: TMFD6 has very few trades per day (~500-1000 ticks). Metaorder detection algorithms are calibrated on markets with 10,000+ trades/day. Sample size may be insufficient.

**Verdict**: **CONDITIONAL APPROVE** — offline research only. Blocked by T1.1. ~150 LOC research script, NOT production.

---

#### T2.2: LO Arrival/Cancel Rate Asymmetry (Event Inference)

**Proposed**: Infer limit order flow dynamics from snapshot diffs (Bechler 2017)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | From snapshot diffs: depth increase at same price = LO arrival, depth decrease = LO cancel or fill. We cannot distinguish cancel from fill without L3 data. |
| **Pipeline integration** | FeatureEngine extension. Uses same prev-state as OFI computation. |
| **Hot-path safety** | O(1). PASS. |

**RISK**: Without L3 data, we cannot distinguish cancellations from executions. The inferred "cancellation rate" is actually "depletion rate" (cancel + fill combined). This is a fundamental data limitation. The paper's key finding (LO flow > MO flow for prediction) relies on separating these.

**Verdict**: **CONDITIONAL APPROVE** — degraded signal quality due to cancel/fill conflation. Same as T1.6 infrastructure. ~40 LOC.

---

#### T2.3: Intensity Burst Detection

**Proposed**: Detect abnormal tick density surges (Christensen 2024)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | Tick timestamps only. Available. |
| **Pipeline integration** | FeatureEngine or separate `BurstDetector` module. Monitor rolling tick rate, flag when > 3x median. |
| **Hot-path safety** | O(1) per tick: update running count, compare to threshold. PASS. |
| **Feature slot** | Binary flag, not continuous feature. Could be a quality flag or separate event. |
| **State** | Rolling tick count (30s window) + median estimate (EMA). ~20 bytes/symbol. |
| **Rust portability** | Trivial. |
| **Config** | `burst.threshold_multiplier: 3.0`, `burst.window_s: 30`. |

**RISK**: TXFD6 at 8 ticks/s may not produce genuine "intensity bursts" as defined in FX markets (100+ ticks/s normal). The threshold must be calibrated for TAIFEX. Too sensitive = too many false positives. Too strict = never triggers.

**Verdict**: **APPROVE** — low implementation cost, clear kill gate. ~50 LOC.

---

#### T2.4: Local Hurst Exponent

**Proposed**: Estimate H_0 from signed trade flow persistence (Muhle-Karbe 2026)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | Requires signed trade flow (T1.1 prerequisite). Even with classification, Hurst estimation requires ~1000+ samples for stability. |
| **Pipeline integration** | Offline research only. Computationally expensive (O(N log N) for DFA/wavelet-based estimators). |
| **State** | Large rolling window of signed returns. |

**RISK**: Hurst exponent estimation is notoriously noisy at short sample sizes. TMFD6 ~1000 ticks/day is marginal. Window of 5 min = ~2400 ticks on TXFD6, might be sufficient but noisy.

**Verdict**: **REJECT for live pipeline. APPROVE for offline research.** Not implementable at tick frequency. ~50 LOC research script.

---

#### T2.5: Spread Widening Duration (Survival Model)

**Proposed**: How long wide spread persists predicts vol regime (Panayi 2014)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | `spread_scaled` (index 3) available every tick. |
| **Pipeline integration** | FeatureEngine. Track time since spread exceeded threshold. Already partially implemented: `tob_survival_ms` (index 18) tracks time since BBO change. Spread duration is analogous. |
| **Hot-path safety** | O(1) comparison + timestamp delta. PASS. |
| **Feature slot** | Slot [25] or [26]: `spread_wide_duration_ms`. |
| **State** | 1 int (timestamp of last spread widening) + 1 int (threshold). 16 bytes/symbol. |

**RISK**: TMFD6 spread is very discrete (1, 2, or 3 ticks). "Widening" events are rare and sudden. The survival model may not have enough resolution.

**Verdict**: **CONDITIONAL APPROVE** — depends on empirical spread transition frequency on TMFD6. ~30 LOC.

---

#### T2.6: LOB KE Approximation

**Proposed**: `KE = sum(delta_depth[i]^2) / dt` (Li 2023)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | L1-L5 depth diffs. L1 available from FeatureEngine state. L2-L5 available from `BidAskEvent.bids/asks` arrays. |
| **Pipeline integration** | FeatureEngine `_compute_values()` or `_compute_mldm()` (which already processes L2-L5). |
| **Hot-path safety** | 5 squared diffs + sum = ~10 ops. PASS. |

**RISK**: R15 already tested LOB KE and found IC too weak. L3-L5 adds noise on TAIFEX. This is a repeat of a killed direction.

**Verdict**: **REJECT** — R15 already killed this. Do not re-investigate.

---

#### T2.7: Event-Driven Aggregation

**Proposed**: Aggregate features between significant price changes instead of fixed windows (Elomari-Kessab 2024)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | All features available. Trigger = mid_price change >= threshold. |
| **Pipeline integration** | AggregationEngine (same module as T0.3). Alternative aggregation mode alongside fixed-window EMAs. |
| **Hot-path safety** | O(1) accumulate, O(1) emit on trigger. PASS. |
| **State** | Accumulators per feature + trigger state. ~200 bytes/symbol. |

**Verdict**: **APPROVE** — natural extension of T0.3 AggregationEngine. ~80 LOC additional.

---

#### T2.8: Persistent Depth Change Ratio

**Proposed**: Filter fleeting depth changes, keep only persistent ones (Filtration 2025)

| Criterion | Assessment |
|-----------|-----------|
| **Data availability** | Snapshot diffs available from existing FeatureEngine state. |
| **Pipeline integration** | FeatureEngine. Track depth changes over N consecutive snapshots. Change that persists for >= 3 snapshots = "real". |
| **Hot-path safety** | O(1) per tick with small state machine. PASS. |
| **State** | Per-symbol: last 3-5 depth values for persistence check. ~40 bytes/symbol. |

**RISK**: Definition of "persistent" is arbitrary. Threshold calibration required. On TMFD6 with 300ms tick interval, "3 snapshots" = 900ms persistence.

**Verdict**: **CONDITIONAL APPROVE** — needs threshold calibration. ~40 LOC.

---

### Tier 3 — High Cost (Brief Assessment)

| Direction | Verdict | Reason |
|-----------|---------|--------|
| Path signatures | **REJECT for live, APPROVE offline** | O(d^k * T) per window. At d=22, k=2, T=240: ~116K ops per 30s window. Feasible offline with `iisignature` library. Not hot-path. |
| Wavelet decomposition | **REJECT** | Implementation complexity, no clear advantage over HAR aggregation. |
| Full PCA mode decomposition | **REJECT** | Requires offline training, model drift risk, heavy engineering. |
| Neural HMM regime | **REJECT for live** | Full model is too heavy. The vol-adaptive gating CONCEPT is captured by T0.1 + T0.3 (volatility-adaptive EMA). |

---

## Part 2: Infrastructure Gap Analysis

### Complete Infrastructure Requirement Matrix

| Gap ID | Description | Blocks | Effort | Priority |
|--------|-------------|--------|--------|----------|
| **INF-1** | `TickEvent.trade_direction` field | T1.1, T1.4, T2.1, T2.4, signed OFI | 100 LOC across 5 files | **P0 CRITICAL** |
| **INF-2** | TickEvent volume access in FeatureEngine | T0.1 (instantaneous vol) | 20 LOC | P1 |
| **INF-3** | `AggregationEngine` module | T0.3, T2.7 | 100-180 LOC new module | P1 |
| **INF-4** | FeatureRegistry v3 bump | T1.3, T1.5, T1.6, T2.5, T2.8 | 30 LOC registry + specs | P2 |
| **INF-5** | ClickHouse schema: `trade_direction` column | T1.1 downstream persistence | 1 migration file | P1 (with INF-1) |
| **INF-6** | `HawkesEstimator` class | T1.2, T2.3 (shared timestamp accumulation) | 50-80 LOC | P2 |

### Directions That Can Proceed with ZERO Infrastructure Changes

| Direction | Why |
|-----------|-----|
| T0.2 Execution Optimizer | Uses existing features. Extends existing `ImbalanceTimer`. |
| T1.3 Sym/Antisym OFI | Decomposes existing OFI computation. No new data needed. |
| T1.7 Log-GOFI | Transform of existing feature. Offline test first. |
| T2.3 Intensity Burst Detection | Uses tick timestamps only. Self-contained module. |
| T2.7 Event-Driven Aggregation | Uses existing feature tuple. |

### Minimal Infrastructure for Maximum Unblock

**INF-1 (trade classification field) alone unblocks 5 directions** and is the foundational investment for the entire "signed flow" research line. Recommend as Week 1, Day 1 priority.

**INF-3 (AggregationEngine) unblocks 2 directions** and is needed for all cross-frequency work. Recommend as Week 1, Day 2-3.

---

## Part 3: Engineering-Driven Proposals

### E1: Ring Buffer Lookback Features (Rust Hot-Path)

**What it computes**: Use the existing `FastTickRingBuffer` / `FastBidAskRingBuffer` to compute lookback features (min/max/mean over last N events) without maintaining separate state.

**Why our stack enables it**: We already have `FastTickRingBuffer` and `FastBidAskRingBuffer` in `rust_core` that store recent events. Currently used only for bus transport. Adding a `compute_lookback_stats(n_events)` method to the Rust ring buffer provides O(N) lookback without any Python-side state management.

**Specific features**:
- `price_range_N` = max(price) - min(price) over last N ticks (volatility proxy)
- `volume_concentration` = max(volume_i) / sum(volume_i) over last N ticks (large trade detection)
- `tick_direction_streak` = consecutive same-direction ticks (momentum proxy)

**LOC estimate**: ~80 LOC Rust (add methods to existing `FastTickRingBuffer`), ~20 LOC Python wrapper.

**Expected value**: Replaces fragile Python ring buffers with zero-copy Rust lookback. Enables features that would be too expensive to maintain in Python EMA state. The `price_range_N` at N=40 (5s) is a direct realized range estimator.

**Verdict**: **HIGH VALUE**. Leverages existing infrastructure. Could provide the instantaneous vol estimate (T0.1) without needing TickEvent volume in FeatureEngine.

---

### E2: ClickHouse Session-Level Feature Aggregation

**What it computes**: Daily/session aggregate features computed via ClickHouse queries and injected at session start:
- `prev_session_realized_vol`: Yesterday's realized volatility for baseline calibration
- `prev_session_avg_spread`: Yesterday's average spread for threshold calibration
- `prev_session_tick_count`: Yesterday's total ticks for activity baseline
- `rolling_5d_vol_regime`: 5-day rolling vol quantile (which regime are we in?)

**Why our stack enables it**: ClickHouse stores full tick history. We already run pre-session queries for position reconciliation (`StartupPositionVerifier`). Adding feature queries is zero-marginal-cost.

**Specific implementation**:
```sql
SELECT
  quantile(0.5)(spread_scaled) as median_spread,
  stddevPop(mid_price_x2) as vol_estimate,
  count() as tick_count
FROM hft.market_data
WHERE symbol = {symbol}
  AND toDate(exch_ts / 1e9) = yesterday()
```

**LOC estimate**: ~50 LOC (query + injection into FeatureEngine as calibration constants).

**Expected value**: Removes hardcoded thresholds from strategies. CBS `_DEFAULT_MOVE_THRESHOLD_BPS = 40` could be calibrated daily based on recent volatility regime. Execution optimizer `spread_threshold_pts` calibrated from actual spread distribution.

**Verdict**: **MEDIUM-HIGH VALUE**. Addresses the known problem of static thresholds in a dynamic market. Does NOT add hot-path latency (computed pre-session).

---

### E3: Fused Normalizer Trade Classification

**What it computes**: Integrate EMO trade classification directly into the `RustNormalizerLobFused` / `RustNormalizerFeatureFusedV1` pipeline, producing classified trades as part of the single Rust call that already handles normalize + LOB + features.

**Why our stack enables it**: The fused normalizer (`HFT_FUSED_NORMALIZER=1`) already processes both tick and bidask events in a single Rust call. Adding trade classification (3 integer comparisons) to the fused path is essentially free -- the data is already in Rust memory, no additional Python-Rust boundary crossing needed.

**Implementation**: Extend `RustNormalizerFeatureFusedV1` to:
1. Cache `best_bid`, `best_ask` from bidask processing
2. On tick processing, compare `trade_price` to cached bid/ask
3. Output `trade_direction` as additional field in the fused result tuple

**LOC estimate**: ~40 LOC Rust, ~10 LOC Python (extract new field from fused tuple).

**Expected value**: Trade classification at zero additional latency on the fused path. Eliminates the need for INF-1 (Python-side implementation) if fused path is enabled. This is the optimal long-term solution.

**Verdict**: **HIGHEST VALUE** for fused-path users. However, requires Rust build, so Python fallback (INF-1) is still needed.

---

### E4: Prometheus Metrics as Slow Features

**What it computes**: Use existing Prometheus counters/histograms as slowly-updated regime features:
- `hft_raw_queue_depth` gauge -> queue pressure regime
- `hft_strategy_latency_ns` histogram -> system load regime
- `hft_fill_rate` (if tracked) -> execution quality regime
- `hft_reconnect_count` -> connectivity stability

**Why our stack enables it**: Prometheus metrics are already being collected. They represent system state that is orthogonal to market microstructure. System degradation (high queue depth, high latency) should trigger conservative trading behavior independently of market state.

**LOC estimate**: ~30 LOC (read current metric values, expose to strategy layer).

**Expected value**: LOW for alpha, HIGH for risk management. System-aware strategy adaptation. Example: if `raw_queue_depth > 100`, CBS should pause entries because market data is stale.

**Verdict**: **MEDIUM VALUE** as a risk layer, not alpha. Recommend as part of StormGuard enhancement rather than alpha research.

---

## Part 4: Implementation Roadmap

### Dependency Graph

```
                     ┌──────────────────┐
                     │   INF-1: Trade   │
                     │ Classification   │
                     │   (TickEvent)    │
                     └──────┬───────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              v             v             v
         T1.4: Sign    T2.1: Meta-   T1.2: Hawkes
         Autocorr.     order Det.    (enhanced w/
                                     signed flow)

  ┌───────────┐    ┌────────────┐    ┌────────────┐
  │ T1.3: Sym │    │ T0.3: HAR  │    │ T0.2: Exec │
  │ /Antisym  │    │ Aggregation│    │ Optimizer  │
  │   OFI     │    │   Engine   │    │            │
  └───────────┘    └─────┬──────┘    └────────────┘
       │                 │
       │                 v
       │           T2.7: Event-
       │           Driven Agg.
       │
       v
  INF-4: Registry
  v3 bump
```

### Phase-Ordered Plan

#### Phase 0: Zero-Cost Quick Wins (Day 1-2)

| Item | LOC | Depends On | Parallelizable |
|------|-----|-----------|----------------|
| T1.3: Sym/Antisym OFI decomposition | 30 | None | Yes |
| T1.7: Log-GOFI (offline IC test) | 5 | None | Yes |
| T0.2: Execution Optimizer extension | 70 | None | Yes |
| T2.3: Intensity Burst Detection | 50 | None | Yes |

All four are independent and can be built in parallel. Total: ~155 LOC.

#### Phase 1: Foundation Infrastructure (Day 2-4)

| Item | LOC | Depends On | Parallelizable |
|------|-----|-----------|----------------|
| INF-1: Trade classification field | 100 | None | With INF-3 |
| INF-3: AggregationEngine | 100 | None | With INF-1 |
| INF-2: TickEvent volume in FeatureEngine | 20 | None | Yes |

INF-1 and INF-3 are independent and can be built in parallel. Total: ~220 LOC.

#### Phase 2: Gate Zero Diagnostics (Day 4-7)

| Item | LOC | Depends On | Parallelizable |
|------|-----|-----------|----------------|
| T1.2: Hawkes branching ratio diagnostic | 50 | None (tick timestamps only) | Yes |
| T1.5: Tick-rate vol vs VRR orthogonality | 20 | None | Yes |
| T1.1 validation: Signed OFI IC comparison | 30 | INF-1 | After INF-1 |
| T0.1: Instantaneous vol feature | 40 | INF-2 | After INF-2 |
| T0.3: HAR aggregation prototype | 100 | INF-3 | After INF-3 |

T1.2 and T1.5 are independent of Phase 1 and can start immediately. T1.1 validation, T0.1, T0.3 depend on Phase 1 completions. Total: ~240 LOC.

#### Phase 3: Dependent Features (Week 2)

| Item | LOC | Depends On |
|------|-----|-----------|
| T1.4: Trade sign autocorrelation | 30 | INF-1 |
| T1.6: Cancellation rate asymmetry | 40 | None |
| T2.7: Event-driven aggregation | 80 | INF-3 |
| T2.5: Spread widening duration | 30 | None |
| INF-4: Registry v3 bump (batch all new features) | 30 | All new features finalized |

Total: ~210 LOC.

#### Phase 4: Research-Only (Week 3-4)

| Item | LOC | Depends On |
|------|-----|-----------|
| E1: Ring buffer lookback (Rust) | 100 | Rust build setup |
| E2: ClickHouse session-level features | 50 | None |
| E3: Fused normalizer trade classification (Rust) | 50 | INF-1 (design), Rust build |
| T2.1: Metaorder detection research script | 150 | INF-1, validated classification |
| T2.8: Persistent depth change ratio | 40 | None |

### Critical Path

```
Day 1:  T1.3 + T1.7 + T0.2 + T2.3 (parallel, zero-dependency)
Day 2:  INF-1 + INF-3 (parallel, foundation)
Day 3:  INF-1 completion + INF-2
Day 4:  T1.1 validation + T0.1 + T0.3 (parallel, depend on INF-1/2/3)
Day 5:  T1.2 + T1.5 Gate Zero diagnostics
Day 6:  Kill gate evaluation. Prune directions with IC < threshold.
Day 7:  Phase 3 features for surviving directions.
Week 2: Integration, backtesting, CBS parameter adaptation.
Week 3: Rust promotions (E1, E3) for surviving features.
```

### Time Estimates Summary

| Phase | Calendar Days | Engineer-Days | LOC |
|-------|--------------|---------------|-----|
| Phase 0 | 1-2 | 1 | 155 |
| Phase 1 | 2-4 | 2 | 220 |
| Phase 2 | 4-7 | 2 | 240 |
| Phase 3 | 8-10 | 2 | 210 |
| Phase 4 | 11-20 | 3-5 | 390 |
| **Total** | **~3 weeks** | **10-12** | **~1,215** |

---

## Appendix A: Final Verdicts Summary

| Direction | Tier | Verdict | LOC | Blocked By |
|-----------|------|---------|-----|-----------|
| T0.1 Instantaneous Vol | 0 | CONDITIONAL | 40 | INF-2 |
| T0.2 Execution Optimizer | 0 | **APPROVE** | 70 | None |
| T0.3 HAR Aggregation | 0 | **APPROVE** | 100 | INF-3 |
| T1.1 Trade Classification | 1 | **APPROVE** | 100 | None (IS the blocker) |
| T1.2 Hawkes Branching | 1 | CONDITIONAL | 50 | Gate Zero |
| T1.3 Sym/Antisym OFI | 1 | **APPROVE** | 30 | None |
| T1.4 Trade Sign Autocorr | 1 | CONDITIONAL | 30 | T1.1 |
| T1.5 Tick-Rate Vol | 1 | CONDITIONAL | 20 | VRR orthogonality |
| T1.6 Cancel Rate Asym | 1 | CONDITIONAL | 40 | Thin book validation |
| T1.7 Log-GOFI | 1 | **APPROVE** | 5 | None |
| T2.1 Metaorder Detection | 2 | CONDITIONAL | 150 | T1.1, offline only |
| T2.2 LO Arrival/Cancel | 2 | CONDITIONAL | 40 | Cancel/fill conflation |
| T2.3 Intensity Burst | 2 | **APPROVE** | 50 | None |
| T2.4 Local Hurst | 2 | REJECT (live) | 50 | T1.1, sample size |
| T2.5 Spread Duration | 2 | CONDITIONAL | 30 | Spread discreteness |
| T2.6 LOB KE Approx | 2 | **REJECT** | - | R15 killed |
| T2.7 Event-Driven Agg | 2 | **APPROVE** | 80 | INF-3 |
| T2.8 Persistent Depth | 2 | CONDITIONAL | 40 | Threshold calibration |
| E1 Ring Buffer Lookback | Eng | **APPROVE** | 100 | Rust build |
| E2 CH Session Features | Eng | **APPROVE** | 50 | None |
| E3 Fused Trade Classif. | Eng | **APPROVE** | 50 | Rust build, INF-1 |
| E4 Prometheus Features | Eng | CONDITIONAL | 30 | Risk layer integration |

**Totals**: 8 full APPROVE, 10 CONDITIONAL, 2 REJECT, 2 REJECT (live only).

---

## Appendix B: Key File References

| File | Role in R22 |
|------|-------------|
| `src/hft_platform/events.py` | `TickEvent` needs `trade_direction` field (INF-1) |
| `src/hft_platform/feed_adapter/normalizer.py` | EMO classification insertion point (INF-1) |
| `src/hft_platform/feature/engine.py` | Sym/antisym OFI (T1.3), vol features (T0.1), v2 state in `_LobKernelState` |
| `src/hft_platform/feature/registry.py` | `lob_shared_v3` definition (INF-4), slots [22]+ |
| `src/hft_platform/execution/imbalance_timer.py` | Extend to full `ExecutionOptimizer` (T0.2) |
| `src/hft_platform/strategies/cascade_bounce.py` | Consumer of vol/regime features, execution optimizer |
| `config/research/latency_profiles.yaml` | 36ms submit RTT constrains execution optimizer design |
| `rust_core/` | E1 (ring buffer), E3 (fused classification) |

---

## Appendix C: Constitution Compliance Check

| Law | Relevant Directions | Status |
|-----|-------------------|--------|
| **Allocator Law** | All hot-path features use pre-allocated `_LobKernelState` fields. No new heap allocation per tick. | PASS |
| **Cache Law** | Feature tuple is contiguous. New features extend tuple, not add new dicts. | PASS |
| **Async Law** | All computations are O(1) per tick. Hawkes calibration (T1.2) is off-hot-path. | PASS |
| **Precision Law** | Trade classification uses integer comparison (`trade_price_x2` vs `mid_x2`). No float on financial path. | PASS |
| **Boundary Law** | E3 (fused trade classification) adds classification to existing Rust call — zero additional boundary crossing. | PASS |
