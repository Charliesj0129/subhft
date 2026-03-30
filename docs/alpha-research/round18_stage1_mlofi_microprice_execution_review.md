# Stage 1 Execution Review: MLOFI Micro-Price Adjustment

**Reviewer**: Execution Reviewer
**Date**: 2026-03-27
**Artifact reviewed**: `docs/alpha-research/round18_stage1_mlofi_microprice.md`
**Direction**: 4.2.1 MLOFI-driven Micro-Price Adjustment (Candidate A recommended)

---

## 1. Latency Profile Assessment

**Source**: `config/research/latency_profiles.yaml` (profile `shioaji_sim_p95_v2026-03-04`)

| Latency Component | Value |
|---|---|
| Pipeline (signal detect + feature compute) | ~250 us |
| MLOFI computation (delta from prior snapshot) | ~1 us (researcher claim) |
| Order modify RTT P95 | 43 ms |
| **Total minimum response time** | **~43.3 ms** |

The researcher claims signal half-life of 1-5 seconds, citing MLOFI gradient IC persistence at ~125ms tick cadence. This claim is **plausible but unverified for the MLOFI micro-price correction specifically**. The Round 11 MLOFI gradient IC = -0.105 was measured at single-tick horizon (125ms), not at multi-second horizons. The decay profile of the correction term has not been empirically measured.

**Edge retention analysis**:
- At 43ms response latency vs 1-second half-life: ~97% of edge retained. Comfortable.
- At 43ms response latency vs 500ms half-life: ~94% of edge retained. Still acceptable.
- At 43ms response latency vs 200ms half-life: ~86% of edge retained. Marginal.
- At 43ms response latency vs 100ms half-life: ~74% of edge retained. **Danger zone.**

**Concern**: The researcher's half-life estimate (1-5s) conflates the MLOFI *gradient* IC persistence with the MLOFI *micro-price correction* half-life. These are different quantities. The correction term (alpha * MLOFI_integrated) adjusts fair value, which means its predictive power depends on the persistence of the *level* of MLOFI, not its IC at tick-by-tick horizons. The half-life must be empirically validated in Stage 2.

**Verdict**: CONDITIONAL PASS. The 500ms kill gate in the researcher's Stage 2 plan is appropriate. If half-life < 200ms, the direction is non-executable at 43ms modify RTT.

For Candidate B (quote width), the latency concern is more severe: by the time an adverse MLOFI signal is detected and quotes widened (43ms), the informed flow may have already filled the resting order. This is acknowledged in the artifact (Risk #2) but not quantified. Candidate B has a **stricter latency requirement** than Candidate A.

---

## 2. Feature Index Mapping

**Source**: `src/hft_platform/feature/registry.py` (`build_default_lob_feature_set_v2`)

### Verified Index Map (lob_shared_v2)

| Index | Feature ID | Researcher Claim | Actual | Match? |
|-------|-----------|-------------------|--------|--------|
| 0 | best_bid | -- | Confirmed | -- |
| 1 | best_ask | -- | Confirmed | -- |
| 2 | mid_price_x2 | -- | Confirmed | -- |
| 3 | spread_scaled | -- | Confirmed | -- |
| 4 | bid_depth | -- | Confirmed | -- |
| 5 | ask_depth | -- | Confirmed | -- |
| 6 | depth_imbalance_ppm | -- | Confirmed | -- |
| 7 | microprice_x2 | Index 7 | Confirmed at index 7 | YES |
| 8 | l1_bid_qty | -- | Confirmed | -- |
| 9 | l1_ask_qty | -- | Confirmed | -- |
| 10 | l1_imbalance_ppm | -- | Confirmed | -- |
| 11 | ofi_l1_raw | -- | Confirmed | -- |
| 12 | ofi_l1_cum | -- | Confirmed | -- |
| 13 | ofi_l1_ema8 | -- | Confirmed | -- |
| 14 | spread_ema8_scaled | -- | Confirmed | -- |
| 15 | depth_imbalance_ema8_ppm | -- | Confirmed | -- |
| 16 | ofi_depth_norm_ppm | Index 16 | Confirmed at index 16 | YES |
| 17 | ret_autocov_5s_x1e6 | -- | Confirmed | -- |
| 18 | tob_survival_ms | -- | Confirmed | -- |
| 19 | impact_surprise_x1000 | -- | Confirmed | -- |
| 20 | deep_depth_momentum_x1000 | -- | Confirmed | -- |

**Researcher's claim**: `mlofi_gradient_x1000` is at index 16. This is **INCORRECT**. Index 16 is `ofi_depth_norm_ppm`. There is **no feature named `mlofi_gradient_x1000` in the registry**. The researcher may be confusing this with `deep_depth_momentum_x1000` at index 20, which is the MLDM feature from Round 11 (multi-level depth momentum). The original Round 11 MLOFI gradient concept was reclassified as MLDM, not as a feature called `mlofi_gradient_x1000`.

**CONFIG DRIFT #1**: The researcher claims `mlofi_gradient_x1000` exists at index 16. It does not exist in the registry at all. Index 16 is `ofi_depth_norm_ppm`. The closest equivalent is `deep_depth_momentum_x1000` at index 20.

**New feature index**: A new `mlofi_microprice_adj_x2` feature would occupy **index 21** in lob_shared_v2, as indices 0-20 are occupied. The researcher correctly states this.

### L5 Access in FeatureEngine

**Critical finding**: The FeatureEngine **does** have access to L5 bid/ask arrays via the `event` parameter passed to `process_lob_update(event, stats, ...)`. The `event` is the original `BidAskEvent` with shape (N,2) bids/asks arrays. This is confirmed by:

1. `_compute_mldm()` (line 638-707) extracts L2-L5 quantities from `event.bids` and `event.asks` via `np.asarray()`.
2. `MarketDataService` passes the original event to `process_lob_update(event, stats, ...)` (line 967).

Therefore, the MLOFI correction could be computed within the FeatureEngine using the same L5 data access pattern as MLDM. This is architecturally feasible.

---

## 3. Config Params Consistency

### Current Strategy Consumption of microprice_x2

**SimpleMarketMaker** (`strategies/simple_mm.py`): Computes its own `micro_price_x2` from `LOBStatsEvent` fields (L1 imbalance * spread * 0.2 coefficient). It does **NOT** consume `microprice_x2` from FeatureEngine events. It receives `LOBStatsEvent` directly and computes internally.

**OpportunisticMM** (`strategies/opportunistic_mm.py`): Has `on_features(event: FeatureUpdateEvent)` method that caches `event.values` into `_feature_cache`. It uses features from `_feature_cache` in `_check_reversal_condition()` for the reversal filter (indices 17, 18, 8, 9). However, it does **NOT** use `microprice_x2` (index 7) from the feature tuple for fair value computation. Its `on_stats()` method receives `LOBStatsEvent` directly for spread gating.

**CONFIG DRIFT #2**: The researcher states (Section 5, "MM Integration Path"): "The adjusted micro-price feeds into the existing MM framework at line 48 of `simple_mm.py`" and "The `OpportunisticMM` strategy can consume this via the FeatureEngine event bus." In reality:
- `SimpleMarketMaker` does NOT use FeatureEngine events at all -- it uses `LOBStatsEvent` directly.
- `OpportunisticMM` caches feature events but does NOT use `microprice_x2` for quoting.
- Neither strategy currently reads a micro-price from the feature tuple for quoting decisions.
- Integration would require modifying strategy code, not just adding a feature.

### OpMM Spread Gate Integration

The researcher proposes the MLOFI correction could integrate with OpportunisticMM's spread gate. The current OpMM spread gate operates on `LOBStatsEvent.spread_scaled` (raw spread from LOB engine), comparing against a **points-based threshold** (recently migrated from bps to points per commit `cfcc534`). The MLOFI correction adjusts **fair value**, not spread. These are orthogonal: spread gate decides IF to quote, fair value adjustment decides WHERE to quote. They are compatible but require separate code paths.

**No config drift here** -- the integration path is viable but requires strategy-level code changes.

---

## 4. Data Pipeline Feasibility

### BidAskEvent L5 Depth

**Source**: `src/hft_platform/events.py` (line 42-56)

`BidAskEvent.bids` and `BidAskEvent.asks` are typed as `Union[np.ndarray, list]` with shape `(N, 2)` where N is the number of levels. The comment says `[[Price, Volume], ...]`. The shape is **not fixed at 5** -- it depends on the broker feed. The researcher claims TXFD6 has L5 via `BidAskEvent.bids/asks` shape (5,2), which is plausible for TAIFEX futures.

### LOBEngine to FeatureEngine Data Flow

The FeatureEngine receives:
1. `stats` (LOBStatsEvent or tuple) -- **L1 summary only** (best_bid, best_ask, spread, imbalance, depths)
2. `event` (the original BidAskEvent or None) -- **contains full L5 arrays**

The `_compute_mldm()` method already demonstrates the pattern of extracting L2-L5 from the `event` parameter. A new MLOFI correction feature could follow the identical pattern.

**Key concern**: When the FeatureEngine is invoked via `process_lob_stats(stats)` (without the event parameter), `event` is None and L5 data is unavailable. This path is used in some calling contexts (see line 969: fallback when `process_lob_update` is not available). In the primary production path (line 967), the event IS passed.

**Verdict**: PASS. L5 data is available in the primary production path. The MLDM feature already implements the extraction pattern. The new feature would follow the same architecture.

### TMFD6 Limitation

The researcher correctly identifies that TMFD6 has L1 only, making direct MLOFI correction inapplicable. The proposed workaround (TXFD6 L5 as proxy for TMFD6) is reasonable since both track the TAIEX index, but introduces cross-instrument dependency and additional latency (TXFD6 update -> compute MLOFI -> adjust TMFD6 fair value). This cross-instrument path does not currently exist in the FeatureEngine architecture (per-symbol state only).

**CONFIG DRIFT #3**: The researcher proposes using TXFD6 L5 MLOFI as a cross-asset signal for TMFD6 quoting. The FeatureEngine operates on per-symbol state (`_states: dict[str, _FeatureState]`, `_lob_kernel_states: dict[str, _LobKernelState]`). There is no cross-symbol feature propagation mechanism. Implementing this would require architectural changes to the FeatureEngine, not just adding a new feature computation. This is not mentioned in the researcher's execution notes.

---

## 5. Risk Limits Assessment

**Source**: `config/base/strategy_limits.yaml`

| Risk Limit | Value | Impact of MLOFI Correction |
|---|---|---|
| max_position_lots | 4 global, 1 per OpMM_TMFD6, 5 per OpMM_TXFD6 | No change -- fair value adjustment does not affect position limits |
| max_order_qty | 1 per order | No change |
| max_daily_loss | 5,000 NTD | No change in limit. However: if the MLOFI correction systematically biases fair value in the wrong direction (e.g., due to TWSE sign inversion error), it could accelerate losses |
| intraday peak_drawdown_pct | 40% | No direct concern |

**Fair value shift magnitude**: The researcher estimates a 2-3 bps correction to fair value. For TXFD6 at ~23,000 points, 3 bps = 6.9 points = ~1,380 NTD per lot. With max_position = 5 on TXFD6, worst-case instantaneous PnL impact from a wrong-sign correction = 5 * 1,380 = 6,900 NTD. This exceeds the daily loss hard limit of 5,000 NTD if sustained.

**Concern**: The TWSE sign inversion (deep = passive = contrarian) is a critical parameter. If the alpha coefficient sign is wrong, the strategy would systematically quote on the wrong side. The researcher's "TWSE sign gate" in the kill gates is appropriate, but this is a non-trivial risk.

**Verdict**: PASS with note. Risk limits are not structurally violated by the proposed approach. The daily loss limit provides a safety net. However, the sign inversion calibration is a high-consequence decision that must be validated empirically.

---

## Config Drift Summary

| # | Parameter | Research Assumption | Production Reality | Drift? |
|---|-----------|--------------------|--------------------|--------|
| 1 | `mlofi_gradient_x1000` at index 16 | Feature exists at index 16 | **Does not exist**. Index 16 is `ofi_depth_norm_ppm`. Closest is `deep_depth_momentum_x1000` at index 20 | **YES -- CRITICAL** |
| 2 | MM strategies consume `microprice_x2` from FeatureEngine | SimpleMarketMaker uses FeatureEngine microprice; OpMM consumes via event bus | SimpleMarketMaker does NOT use FeatureEngine events. OpMM caches feature events but does NOT use `microprice_x2` for quoting. | **YES -- MODERATE** |
| 3 | Cross-symbol MLOFI (TXFD6 -> TMFD6) feasible via FeatureEngine | TXFD6 L5 can adjust TMFD6 fair value | FeatureEngine is per-symbol with no cross-symbol propagation mechanism | **YES -- MODERATE** |
| 4 | Signal half-life 1-5 seconds | Based on MLOFI gradient IC persistence | Unverified for MLOFI micro-price correction specifically. MLOFI gradient IC was measured at single-tick horizon, not multi-second decay. | **UNVERIFIED** |
| 5 | Latency budget "comfortable" | 36ms submit, signal >> RTT | For MM modify scenario, 43ms modify RTT is the binding constraint, not 36ms submit | **MINOR** |

---

## Overall Assessment

### REJECT (with conditions for re-submission)

**Rationale**: Three config drift items found (1 critical, 2 moderate).

**Critical drift (#1)**: The researcher references a non-existent feature (`mlofi_gradient_x1000` at index 16). This suggests the researcher is working from outdated or incorrect knowledge of the feature registry. The feature registry must be correctly referenced for any prototype to be built. The actual MLDM feature at index 20 computes multi-level depth momentum (fast EMA - slow EMA of L2-L5 depth changes), which is related to but not identical to the MLOFI gradient concept described in the papers. The researcher should clarify whether they intend to use the existing MLDM feature, extend it, or create a new feature.

**Moderate drift (#2)**: The integration path described is inaccurate. Neither SimpleMarketMaker nor OpportunisticMM currently consume micro-price from FeatureEngine events. Strategy modification is required for integration. This affects effort estimation.

**Moderate drift (#3)**: The TMFD6 application path via cross-symbol propagation requires FeatureEngine architectural changes not acknowledged in the artifact.

### Conditions for Re-Approval

1. **Fix feature registry references**: Correctly identify existing features (MLDM at index 20, `ofi_depth_norm_ppm` at index 16). Clarify relationship between proposed MLOFI correction and existing MLDM.
2. **Revise integration path**: Acknowledge that strategy-level code changes are required. Specify which strategy will be modified first (OpMM is the natural candidate since it already has `on_features()`).
3. **Scope TMFD6 as Phase 2**: Defer cross-symbol MLOFI propagation. Prototype on TXFD6 (direct L5) and 2330 (direct L5) only. TMFD6 integration requires separate design work.
4. **Add half-life measurement to Stage 2 protocol**: Explicitly measure the autocorrelation decay of the MLOFI correction term (not just the MLOFI gradient IC), and compare against the 43ms modify RTT (not 36ms submit RTT).

If these corrections are made, the direction is fundamentally sound. The FeatureEngine architecture supports L5 feature computation (proven by MLDM), and Candidate A's linear formulation is tractable.
