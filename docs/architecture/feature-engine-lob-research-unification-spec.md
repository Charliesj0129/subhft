# Feature Engine / LOB / Research Unification Spec (Draft)

Date: 2026-02-24
Status: Draft (prototype partially implemented; FE-01..FE-05 landed, FE-06/FE-07 advanced prototypes landed)
Owner: Architecture / Research / Runtime
Scope: Research (`hftbacktest`), runtime market-data/LOB, strategy consumption, Rust/PyO3 boundary

## 1. Problem Statement

The current system supports:
- live/sim runtime market-data normalization + `LOBEngine`
- strategy consumption via `TickEvent` / `BidAskEvent` / `LOBStatsEvent`
- research backtests and `hftbacktest` execution simulation

However, feature engineering is still fragmented:
- some features are computed in strategy code (runtime)
- some features are computed in research-only code
- `hftbacktest` adapter currently focuses on strategy event simulation rather than shared feature-kernel parity

This creates three risks:
1. Research/live feature drift (different implementations / reset rules / timestamp semantics)
2. Strategy hot-path CPU waste (shared microstructure features recomputed per strategy)
3. Low ROI on Rust optimization (optimized LOB state exists, but features stay in Python strategy logic)

## 2. Decision Summary

Introduce a **Feature Plane** with a dedicated `FeatureEngine` layered after `LOBEngine`.

Target runtime flow (planned):
- `Normalizer -> LOBEngine -> FeatureEngine -> StrategyRunner -> Risk -> OrderAdapter`

Target research/hftbacktest flow (planned):
- `hftbacktest data/depth -> HftBacktestAdapter(lob_feature mode) -> LOBEngine -> FeatureEngine -> strategy`

Core principle:
- **Shared microstructure features** move to `LOBEngine`/`FeatureEngine` (Rust-first when stable)
- **Strategy-specific decision logic** remains in strategy
- **Research and live use the same feature kernel semantics**

## 3. Goals and Non-Goals

### Goals

1. Unify feature semantics across research / replay / hftbacktest / live.
2. Reduce strategy hot-path compute and allocations by moving shared feature computation upstream.
3. Enable Rust optimization of feature kernels with explicit parity gates.
4. Keep rollout incremental and reversible (feature flags + fallback).

### Non-Goals (v1)

1. Rewriting all strategy APIs in one step.
2. Moving strategy decision logic into `LOBEngine`.
3. Implementing full zero-copy feature transport on day one.
4. Supporting every possible research-only feature in runtime.

## 4. Architectural Placement Rules (Feature Placement Policy)

Use the following rules to decide where a feature belongs.

### Place in `FeatureEngine` (shared runtime/research kernel)

Features that are:
1. event-driven and updated frequently (tick / book / trade cadence)
2. shared across multiple strategies
3. low-state or bounded-state (O(1) or small rolling windows)
4. semantically tied to LOB/tick microstructure

Examples:
- spread / mid / microprice
- queue imbalance / depth imbalance
- OFI and short-window OFI variants
- short rolling zscore / std / EMA over microstructure signals
- regime flags derived from LOB microstructure state

### Keep in Strategy / Research Layer

Features that are:
1. strategy-specific combinations / decision thresholds
2. cross-symbol / cross-asset aggregations
3. slow-batch statistical transforms or heavy offline factors
4. exploratory prototypes not yet stable enough for runtime governance

## 5. Planned Component Model (Target, TODO)

## 5.1 New Component: `FeatureEngine` (TODO)

Responsibilities:
1. consume normalized market-data / LOB updates
2. update per-symbol feature state
3. expose current feature values (cache/view API)
4. optionally emit `FeatureUpdate` frames/events to strategy plane
5. enforce warmup/reset/gap semantics consistently

Non-responsibilities:
1. order/risk decisions
2. broker/execution I/O
3. persistence of full feature history (recorder can subscribe separately)

## 5.2 `LOBEngine` Responsibilities (Remain Narrow)

`LOBEngine` should remain focused on:
1. book state updates
2. core book statistics
3. low-level state access (`get_l1_scaled`, snapshots, stats emit)

`LOBEngine` should not become a catch-all for strategy/business logic.

## 6. Feature ABI v1 (Implementable Contract)

This ABI is the contract between `FeatureEngine` and strategy/research consumers.

## 6.1 Feature Set Metadata

Each deployed feature bundle must have:
- `feature_set_id` (string, versioned)
- `feature_schema_version` (int)
- `features[]` metadata list:
  - `feature_id`
  - `name`
  - `dtype` (`i64`, `f64`, `u32`, etc.)
  - `scale` (for scaled-int features; `0` if N/A)
  - `warmup_min_events`
  - `source_kind` (`book`, `tick`, `trade`, `mixed`)
  - `flags` (optional behavior bits)

## 6.2 Feature Update Frame (Conceptual ABI)

`FeatureUpdateFrameV1`
- `marker`: `"feature_update_v1"`
- `symbol`: `str` (v1; can move to `symbol_id` later)
- `seq`: `int`
- `source_ts_ns`: `int`
- `local_ts_ns`: `int` (optional in research; required in runtime)
- `feature_set_id`: `str`
- `changed_mask`: `int` or bitset bytes
- `warmup_ready_mask`: `int` or bitset bytes
- `quality_flags`: `int` bitmask
- `values`: packed arrays / tuple (schema-ordered)

### `quality_flags` (initial bits)
- bit 0: `GAP_DETECTED`
- bit 1: `STATE_RESET`
- bit 2: `STALE_INPUT`
- bit 3: `OUT_OF_ORDER_INPUT`
- bit 4: `PARTIAL_FEATURES`

## 6.3 Precision Rules

1. Price-derived features should prefer scaled integers when exactness matters.
2. Statistical normalized outputs (`zscore`, `imbalance ratio`) may use `float64`.
3. ABI metadata must declare scaling explicitly.
4. Research and runtime must use the same scaling semantics for shared features.

## 6.4 Timestamp / Ordering Rules

1. `source_ts_ns` is the canonical ordering timestamp when available.
2. If source timestamps are missing or duplicated, sequence ordering must still be deterministic.
3. `FeatureEngine` must define behavior for:
- duplicate seq
- out-of-order seq
- missing seq / gap
- session reset / reconnect reset

## 7. Strategy Consumption Model (Incremental Rollout)

## 7.1 Phase A: Pull API (Preferred First Step)

Add `StrategyContext` feature accessors (planned):
- `get_feature(symbol, feature_id)`
- `get_feature_view(symbol)` (schema-ordered view + metadata)
- `get_feature_set_id()`

Benefits:
1. low migration risk
2. existing strategy dispatch remains intact
3. easy parity testing against legacy strategy-computed features

## 7.2 Phase B: Push API (High-Performance)

Add optional strategy handler:
- `on_features(feature_update)`

Use when:
- strategy only cares about feature updates
- minimizing event fanout and conditional dispatch becomes important

Rollout rule:
- push API is optional until feature plane semantics and parity stabilize

## 8. `hftbacktest` Integration Spec (Feature-First Backtest)

## 8.1 Adapter Modes (Planned)

`HftBacktestAdapter` modes:
1. `stats_only` (existing compatibility mode)
2. `lob_feature` (new mode, TODO)

## 8.2 `lob_feature` Mode Flow

1. read depth/tick data from `hftbacktest`
2. convert to platform-normalized tuple/event contract
3. call `LOBEngine.process_event(...)`
4. call `FeatureEngine.process_*` / `FeatureEngine.on_lob_update(...)`
5. strategy reads features (pull) or receives `FeatureUpdate`
6. `hftbacktest` continues to handle order execution simulation

## 8.3 Backtest/Live Parity Principle

For shared features:
- same feature kernel
- same warmup/reset rules
- same precision/scaling
- same feature IDs and schema ordering

Allowed differences (must be documented):
- missing venue-specific fields in backtest data
- `hftbacktest` event granularity limitations (snapshot-only vs incremental)

## 9. Feature Governance Workflow (Strict)

## Phase 0 - Feature Spec & Placement Review

Inputs:
- alpha hypothesis
- proposed feature list

Outputs:
1. `Feature Spec` docs (one per feature or feature family)
2. placement decision (`FeatureEngine` vs strategy/research)
3. expected precision and warmup/reset semantics

Gate:
- no strategy-specific decision logic in feature kernel

## Phase 1 - Python Reference (Research)

Outputs:
1. Python reference implementation (research-only)
2. unit tests for edge cases
3. first backtest evidence

Gate:
- deterministic outputs on replayed sample

## Phase 2 - FeatureEngine Kernel (Python wrapper + Rust-ready contract)

Outputs:
1. `FeatureEngine` interface and registry entries
2. runtime-compatible feature state implementation
3. parity harness vs Python reference

Gate:
- parity within tolerance for all tested scenarios

## Phase 3 - `hftbacktest` Integration

Outputs:
1. `HftBacktestAdapter` `lob_feature` mode
2. feature-mode replay/backtest tests
3. performance benchmarks (stats_only vs lob_feature)

Gate:
- no semantic drift for shared features

## Phase 4 - Strategy Migration

Outputs:
1. strategy feature pull API
2. migrated example strategies
3. A/B parity report (legacy compute vs feature-consume)

Gate:
- strategy decisions match within defined tolerance/logic parity

## Phase 5 - Live Shadow / Canary

Outputs:
1. shadow parity metrics
2. canary rollout runbook
3. rollback criteria

Gate:
- stable parity + no loop-stall regression + acceptable queue lag

## 10. Testing and Validation Matrix (Required)

### 10.1 Correctness / Parity

1. Python reference vs FeatureEngine (per-event parity)
2. FeatureEngine Python vs Rust kernel parity
3. replay vs `hftbacktest` parity (same input snapshot stream where possible)
4. strategy legacy-path vs feature-consume-path decision parity

### 10.2 Performance

1. `LOBEngine` only baseline
2. `LOBEngine + FeatureEngine` overhead
3. strategy decision latency before/after feature offload
4. `hftbacktest` throughput in `stats_only` vs `lob_feature`

### 10.3 Failure Mode Drills

1. gap/reset handling
2. out-of-order event handling
3. feature warmup incomplete behavior
4. feature schema mismatch (`feature_set_id`) behavior

## 11. TODO Backlog (Implementation Work Packages)

Status legend:
- `✅ LANDED`: implemented in codebase (may still be prototype-scoped)
- `🔄 TODO`: not started
- `🧪 SPEC`: design/testing definition complete, no code yet
- `🟡 PROTOTYPE`: initial implementation landed; not production-complete

### 11.0 Current As-Built Snapshot (What Is Done Now)

The following items are already implemented in the codebase (prototype scope unless stated otherwise):

1. `FeatureUpdateEvent` runtime event and feature schema metadata/registry (`FeatureRegistry`, default `lob_shared_v1`)
2. `FeatureEngine` prototype with per-symbol state, reset hooks, quality flags, and cache/view APIs
3. Runtime wiring: `MarketDataService` can run `LOBEngine -> FeatureEngine` under feature flag (`HFT_FEATURE_ENGINE_ENABLED`)
4. Bootstrap/registry wiring: `SystemBootstrapper` and `ServiceRegistry` can instantiate and hold `feature_engine`
5. Strategy consumption (pull): `StrategyContext.get_feature(...)`, `get_feature_view(...)`, `get_feature_set_id()`
6. Strategy push hook (optional prototype): `BaseStrategy.on_features(...)`
7. `hftbacktest` integration prototype: `HftBacktestAdapter(feature_mode=\"lob_feature\")` with L1 synthesized `BidAskEvent -> LOBEngine -> FeatureEngine`
8. Shared feature kernels (prototype): spread/mid/microprice/depth imbalance + L1 OFI (`raw/cum/ema8`) + bounded EMA proxies
9. Runtime feature observability skeleton: feature-plane latency/update/quality metrics + MarketDataService sampling
10. Feature parity/perf scaffolding (Python path): unit-test reference parity + perf gate feature benchmarks / mismatch-rate metric
11. Typed feature boundary (Python prototype): `TypedFeatureFrameV1` pack/unpack helpers for Python/Rust ABI transition
12. Runtime shadow parity compare path (sampled, feature-flagged) + prototype runbook for canary/reset/gap/schema incidents

Not done yet (still TODO):

1. Production-ready Rust/PyO3 feature kernels for promoted feature families (beyond prototype v1 kernel)
2. Finalized typed feature frame transport (zero-copy/packed transport path)
3. Full shadow/canary governance automation (dashboards/alerts/decision enforcement)

### FE-01 Feature ABI v1 and Registry
- `✅ LANDED` `FeatureUpdateEvent`/ABI-like runtime event landed (Python; v1 prototype contract)
- `✅ LANDED` `FeatureRegistry` implemented in runtime (default `lob_shared_v1` feature set)
- `🟡 PROTOTYPE` ABI still uses `symbol: str` and Python tuple payloads (typed Rust frame not yet defined)

### FE-02 FeatureEngine Skeleton
- `✅ LANDED` `FeatureEngine` interface (Python) added after `LOBEngine` in runtime path (feature-flagged)
- `✅ LANDED` Per-symbol feature state store and reset hooks added (basic quality flags + out-of-order handling)
- `🟡 PROTOTYPE` Python implementation only; no Rust kernel boundary yet

### FE-03 Strategy Consumption (Pull API)
- `✅ LANDED` `StrategyContext` feature getters added (`get_feature`, `get_feature_view`)
- `✅ LANDED` Feature-set version exposure added (`get_feature_set_id`)
- `🟡 PROTOTYPE` Optional push hook exists (`on_features`) but rollout remains pull-first

### FE-04 `hftbacktest` Adapter Feature Mode
- `✅ LANDED` `lob_feature` mode added to `HftBacktestAdapter`
- `✅ LANDED` Adapter can route `BidAskEvent -> LOBEngine -> FeatureEngine` (L1 synthesized path)
- `🟡 PROTOTYPE` Current path is L1 synthesized compatibility mode (not full incremental depth parity)

### FE-05 Shared Feature Kernels (Priority Set)
- `✅ LANDED` microprice / spread / depth imbalance kernel implemented (FeatureEngine v1 default set)
- `✅ LANDED` L1 OFI-style kernels added (`ofi_l1_raw/cum/ema8`) using bounded state
- `✅ LANDED` bounded-state EMA features added (spread/depth-imbalance EMA proxies)
- `🟡 PROTOTYPE` Python kernels only; promoted feature set not yet Rust-backed

### FE-06 Rust/PyO3 Boundary
- `✅ LANDED` Python reference parity unit-test coverage added for current FeatureEngine kernels
- `✅ LANDED` perf-gate metrics added (`feature_engine_lob_stats_us_per_event`, `feature_engine_lob_update_us_per_event`, `feature_engine_parity_mismatch_rate`)
- `✅ LANDED` Typed feature boundary prototype (`TypedFeatureFrameV1` + event/frame pack/unpack helpers) defined on Python side
- `🟡 PROTOTYPE` Rust `LobFeatureKernelV1` kernel path integrated behind `FeatureEngine(kernel_backend=\"rust\")`
- `🟡 PROTOTYPE` Optional perf-gate Rust drills added (`--include-feature-rust`) incl. Python-vs-Rust parity mismatch metric
- `🔄 TODO` Finalize typed feature frame boundary for Python/Rust transport (packed/zero-copy transport path)
- `🔄 TODO` Promote Rust kernel path from prototype to production-ready for selected feature families

### FE-07 Rollout Governance
- `✅ LANDED` Runtime feature-plane latency/update/quality metrics skeleton added (sampling + counters)
- `✅ LANDED` Shadow parity metric names registered and runtime sampled compare/emit path implemented in `MarketDataService` (feature-flagged)
- `✅ LANDED` Prototype runbook added for shadow/canary + reset/gap/schema mismatch incidents
- `🟡 PROTOTYPE` Canary criteria are documented but not enforced by runtime/ops automation
- `🔄 TODO` Add dashboard/alert wiring and canary decision automation

## 12. Dependency Order (Recommended Execution Sequence)

1. `FE-01` Feature ABI + registry spec finalization
2. `FE-02` FeatureEngine skeleton (Python)
3. `FE-03` Strategy pull API
4. `FE-04` `hftbacktest` `lob_feature` mode
5. `FE-05` First shared feature kernels (Python reference + runtime)
6. `FE-06` Rust/PyO3 kernel migration + parity gate
7. `FE-07` shadow/canary rollout and runbook

## 13. Open Questions (Must Be Resolved Before FE-04/FE-06)

1. Does `hftbacktest` provide sufficient incremental depth events for all target features, or do we support snapshot-only compatibility subsets?
2. Should `FeatureUpdateFrameV1` start with `symbol: str` (simpler) or `symbol_id: int` (faster, harder migration)?
3. Which feature families require scaled-int outputs vs `float64` in v1?
4. Is strategy v1 consumption pull-only, or do we commit to push `on_features()` in the first rollout?

## 14. Definition of Done (Program-Level)

This initiative is considered delivered when all conditions are true:

1. At least one production strategy consumes shared features without recomputing them locally.
2. Research replay / `hftbacktest` / live share the same feature kernel semantics for the promoted feature set.
3. Rust kernel parity gate exists and passes for promoted feature kernels.
4. Strategy hot-path CPU/latency improves measurably versus legacy local feature computation.
5. Rollout runbook and parity dashboards exist for operations.
