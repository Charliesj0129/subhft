# Round 20 — Stage 1 Execution Review: L2 LOB Data-Driven Strategies

**Date**: 2026-03-27
**Reviewer**: Claude (Execution Reviewer agent)
**Survey reviewed**: `docs/alpha-research/round20_stage1_l2_lob_survey.md`

---

## Candidate A: Cross-Asset L5 OFI (2330 -> TXFD6)

### 1. Latency Assessment: CONDITIONAL PASS

**Chain latency analysis**:
- 2330 quote arrives via Shioaji callback -> `raw_queue` -> normalize -> LOB -> FeatureEngine: ~250 us (internal pipeline, per `latency_profiles.yaml`)
- TXFD6 quote arrives same path: ~250 us
- Cross-asset OFI computation (PCA dot product on L5 vectors): ~10-50 us (simple linear algebra)
- Strategy decision: ~50-100 us
- Order submission P95 RTT: **36 ms** (Shioaji sim)
- **Total chain: ~37 ms** from 2330 quote to TXFD6 order ack

**Signal half-life vs latency**:
- Survey claims 30-120s horizon. At 37 ms total chain latency, this leaves >99.9% of signal lifetime for execution.
- R17 found TSMC->TXFD6 IC=0.061 at L1. The cross-asset lag is on the order of seconds, not milliseconds.
- **Verdict**: Latency is NOT the bottleneck for this signal. The 37 ms chain is negligible relative to the 30-120s signal horizon.

**Temporal alignment concern**:
- 2330 (TSE stock) ticks at irregular intervals (quote-driven).
- TXFD6 median tick interval = 125 ms.
- Both arrive through the same Shioaji subscription. Alignment is feasible at the event-loop level, but the FeatureEngine computes features **per-symbol independently** -- there is no cross-symbol feature computation today. See Feature Engine assessment below.

### 2. Feature Engine Assessment: NEEDS EXTENSION

**Current state** (`feature/registry.py`, `feature/engine.py`):
- FeatureEngine v2 computes 21 features (indices 0-20), all **single-symbol, single-LOB** derived.
- `_states` and `_lob_kernel_states` are keyed by symbol (per-symbol independent state).
- `process_lob_update()` processes one symbol's LOBStatsEvent at a time. No cross-symbol state access.
- No PCA or multi-level OFI integration exists in the registry.

**What Candidate A requires**:
1. **Multi-level OFI computation**: L1-L5 OFI per symbol. Currently only `ofi_l1_raw/cum/ema8` exist (indices 11-13). Need L2-L5 OFI. This requires L5 depth data flowing through the live pipeline (see Data Pipeline below).
2. **Cross-symbol feature**: The engine would need to access 2330's feature state when processing TXFD6 events (or vice versa). This is a **new architectural pattern** -- the current engine is strictly per-symbol.
3. **PCA coefficients**: Pre-computed PCA weights from research must be loaded as config. This is straightforward (similar to EMA alpha parameters).

**Extension effort**: Medium-High. Cross-symbol state access in FeatureEngine is a new pattern requiring careful design to avoid violating the per-symbol isolation model.

### 3. Data Pipeline Assessment: NEEDS WORK

**Multi-symbol subscription**:
- `config/symbols.yaml` already includes both `2330` (TSE stock) and `TXFD6` (FUT future). Both are subscribed simultaneously via `subscribe_basket`.
- `MarketDataService` processes all symbols through the same `raw_queue` -> normalize -> LOB -> FeatureEngine pipeline.
- `StrategyRunner.process_event()` dispatches events to all strategies regardless of symbol (no symbol filtering at dispatch level). A strategy with `symbols = {"2330", "TXFD6"}` will receive events for both.
- **Verdict**: Multi-symbol data delivery is already supported.

**L5 data in live pipeline**:
- `LOBEngine.books` stores `BookState` per symbol. `BookState` maintains multi-level book state.
- `LOBStatsEvent` exposes L1 summary stats (mid, spread, imbalance, bid/ask depth).
- L5 depth arrays are available in `BidAskEvent.bids/asks` (shape (N,2) numpy arrays).
- **Gap**: The FeatureEngine only consumes `LOBStatsEvent` (L1 summary), not the raw L5 `BidAskEvent`. Computing L5 OFI requires either:
  - (a) Extending FeatureEngine to also consume `BidAskEvent` (access to full L5 arrays), or
  - (b) Extending `LOBStatsEvent` to include L2-L5 fields.
- **Verdict**: L5 data exists in the pipeline but is not surfaced to the FeatureEngine. Moderate plumbing work required.

### 4. Risk Framework Assessment: COMPATIBLE

- `strategy_limits.yaml` already supports per-strategy position limits.
- A new `CROSS_ASSET_OFI_TXFD6` strategy entry would need:
  - `max_position: 1` (single lot, conservative)
  - `max_order_qty: 1`
- Existing `global_defaults.max_position_lots: 4` has headroom (current: OpMM_TX + OpMM_TMF + CBS/VRM mutex = 3 max concurrent).
- Risk validators, StormGuard, circuit breakers all operate per-strategy and per-symbol -- no conflict.
- **Verdict**: Fully compatible. Config-only addition.

### 5. Overall: CONDITIONAL APPROVE

**Required conditions**:
1. Research phase uses offline L5 data only (no live pipeline changes needed for prototype).
2. If research IC > 0.05 detrended, then design doc for cross-symbol FeatureEngine extension before live deployment.
3. Verify 2330/TXFD6 L5 temporal overlap in research data (different exchanges may have different trading hours edge cases).
4. PCA coefficients must be computed on training data only; walk-forward validation mandatory.

---

## Candidate B: LOB Shape Regime Detection via Snapshot Clustering

### 1. Latency Assessment: PASS

- Single-symbol computation (TXFD6 only). No cross-asset latency chain.
- K-means cluster assignment on a 10-dimensional vector (5 bid + 5 ask volumes): ~1-5 us.
- Regime lookup + OFI gating: ~1 us.
- **Total additional latency**: < 10 us on top of existing pipeline.
- Signal horizon 30-60s. Latency is negligible.

### 2. Feature Engine Assessment: NEEDS EXTENSION

**What Candidate B requires**:
1. **L5 volume profile features**: 10 values (bid_qty_l1..l5, ask_qty_l1..l5). Currently only `l1_bid_qty` [8] and `l1_ask_qty` [9] are in the registry. L2-L5 quantities are NOT registered as features.
2. **Cluster assignment**: Pre-computed cluster centroids loaded as config. Online assignment is a simple argmin over Euclidean distances -- trivial compute.
3. **Regime indicator feature**: A new `lob_regime_id` feature (integer 0..K-1) in the registry.

**Same L5 gap as Candidate A**: FeatureEngine does not consume `BidAskEvent` today. L2-L5 depth quantities are not surfaced.

**Extension effort**: Medium. Less than Candidate A (no cross-symbol), but still requires the L5-to-FeatureEngine plumbing.

### 3. Data Pipeline Assessment: NEEDS WORK (same L5 gap)

- Same issue as Candidate A: L5 depth is available in `BidAskEvent` but not surfaced to FeatureEngine.
- Single-symbol only, so no cross-asset alignment issues.
- 10 days of L5 data is marginal for cluster calibration. **Overfitting risk is the primary concern**, not pipeline feasibility.

### 4. Risk Framework Assessment: COMPATIBLE

- Would operate as a **filter on existing strategies** (CBS, VRM), not standalone.
- No new position limits needed -- gating reduces trade frequency, does not increase exposure.
- Config change: add `regime_filter_enabled: true` to existing strategy configs.

### 5. Overall: CONDITIONAL APPROVE (DEPRIORITIZE)

**Required conditions**:
1. Must demonstrate regime stability across at least 5 independent days before live consideration.
2. Cluster count K must be fixed and justified (not optimized on test data).
3. R15 finding (L3-L5 noise on TXFD6 thin book) must be explicitly addressed -- show that clustering is robust to L3-L5 noise.
4. Implementation as filter only, not standalone strategy.

**Deprioritize rationale**: Highest overfit risk (10 days + clustering hyperparameters), and prior R15 negative result on L3-L5 features is not fully mitigated.

---

## Candidate C: Trade Co-occurrence Conditional OFI

### 1. Latency Assessment: PASS

- Single-symbol, L1-only computation. No cross-asset or L5 dependency.
- Co-occurrence classification: compare current trade timestamp against recent trade timestamps. ~1-5 us.
- COI computation: running OFI split by classification. ~1-2 us.
- **Total additional latency**: < 10 us.
- Signal horizon 30-120s. Latency is negligible.

### 2. Feature Engine Assessment: NEEDS EXTENSION (minor)

**What Candidate C requires**:
1. **Trade event consumption**: FeatureEngine currently only consumes `LOBStatsEvent`. COI needs `TickEvent` (trade data: timestamp, price, volume).
2. **New features**: `coi_isolated_raw`, `coi_clustered_raw`, `coi_differential` (3 new feature slots).
3. **Co-occurrence window parameter**: Configurable threshold for "isolated" vs "clustered" classification.

**Key concern**: FeatureEngine is LOB-stats-driven. Adding `TickEvent` consumption is a new input channel. However, the `process_lob_update()` method already accepts a generic `event` parameter alongside `stats`, suggesting the interface was designed with extensibility in mind.

**Extension effort**: Low-Medium. Simpler than A or B because it uses L1 data only.

### 3. Data Pipeline Assessment: FEASIBLE

- `TickEvent` already flows through the full pipeline (normalize -> bus -> StrategyRunner).
- 40+ days of L1 trade data available for research -- sufficient for walk-forward validation.
- **No L5 dependency**. No cross-asset dependency.
- **Trade-side classification gap**: TAIFEX does not provide trade initiator side. Must infer from tick rule (price vs previous price) or quote rule (price vs mid). This is a known limitation but standard practice.
- **Verdict**: Most feasible of the three candidates from a data pipeline perspective.

### 4. Risk Framework Assessment: COMPATIBLE

- Best suited as CBS/VRM filter (same as B). No new position limits.
- If used standalone, would need a new strategy entry with `max_position: 1`.
- Fits within existing `global_defaults.max_position_lots: 4` headroom.

### 5. Overall: CONDITIONAL APPROVE

**Required conditions**:
1. Define co-occurrence window threshold with clear justification (not just optimized on data).
2. Trade-side classification method must be documented and tested for accuracy.
3. Detrended IC gate mandatory (as per survey kill conditions).
4. Validate that "isolated" vs "clustered" classification is stable at TXFD6's 125 ms median tick interval -- the boundary between isolated and clustered may be blurry.
5. Daily -> intraday adaptation from Lu et al. (2022) must be explicitly justified.

---

## Config Drift Check

| Item | Expected | Actual | Status |
|------|----------|--------|--------|
| `strategy_limits.yaml` max_position_lots | 4 | 4 | OK |
| `symbols.yaml` has 2330 | yes | yes (code: '2330') | OK |
| `symbols.yaml` has TXFD6 | yes | yes (code: TXFD6) | OK |
| Feature registry default | `lob_shared_v2` | `lob_shared_v2` | OK |
| Feature count v2 | 21 | 21 (indices 0-20) | OK |
| Shioaji P95 RTT | ~36 ms | 36.0 ms | OK |
| Internal pipeline latency | ~250 us | 250 us | OK |
| **Config drift** | 0 | **0** | PASS |

---

## Summary Verdict

| Candidate | Latency | Feature Engine | Data Pipeline | Risk | Overall |
|-----------|---------|----------------|---------------|------|---------|
| **A**: Cross-Asset L5 OFI | CONDITIONAL PASS | Needs extension (cross-symbol + L5) | Needs work (L5 plumbing) | Compatible | **CONDITIONAL APPROVE** |
| **B**: LOB Shape Regime | PASS | Needs extension (L5 features) | Needs work (L5 plumbing) | Compatible | **CONDITIONAL APPROVE (DEPRIORITIZE)** |
| **C**: Trade Co-occurrence COI | PASS | Needs extension (minor: TickEvent) | Feasible | Compatible | **CONDITIONAL APPROVE** |

### Execution Priority Recommendation

1. **C first** -- lowest implementation risk, uses existing L1 data, 40+ days for validation, smallest FeatureEngine extension. Best risk/reward for engineering effort.
2. **A second** -- highest IC potential but requires cross-symbol FeatureEngine pattern (new architecture). Research phase is feasible with offline data; live deployment requires design doc.
3. **B deprioritized** -- shares A's L5 plumbing requirement but with higher overfit risk and lower expected IC. Revisit when more L5 data accumulates.

### Shared Infrastructure Dependency

Both A and B require **L5 depth data surfaced to FeatureEngine**. If either is approved for live deployment, this plumbing work benefits both. Consider scheduling as a shared infrastructure task if A passes research gates.
