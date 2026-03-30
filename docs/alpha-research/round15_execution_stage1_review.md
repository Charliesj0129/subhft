# Round 15 — Stage 1 Execution Review (Tradability)

**Reviewer**: Execution Agent
**Date**: 2026-03-25
**Survey reviewed**: `docs/alpha-research/round15_stage1_survey.md`

---

## Platform State of Record

- **FeatureEngine**: `lob_shared_v1`, schema_version=1, **16 features** (indices 0-15). The survey references "FeatureEngine v2: 18 features" but `lob_shared_v2` does **not exist in codebase** — ISS [16] and MLDM [17] are documented in memory as provisional but have no implementation in `src/hft_platform/feature/`. This is a factual correction: any candidate referencing indices 16-17 as existing infrastructure is building on unimplemented code.
- **FeatureEngine input**: `process_lob_update(event, stats)` receives the raw `BidAskEvent` and `LOBStatsEvent`. Currently, only L1 quantities are extracted from `BidAskEvent.bids/asks` via `_extract_l1_qty()` (line 550-581 of `engine.py`). L2-L5 depth data is **not consumed** by the engine today.
- **BidAskEvent**: `bids: np.ndarray shape (N,2)`, `asks: np.ndarray shape (N,2)` — price (col 0, scaled x10000), qty (col 1). Up to L5 depth. `stats` tuple and `fused_stats` tuple available.
- **TickEvent**: `price: int` (x10000), `volume: int`, `total_volume`, `bid_side_total_vol`, `ask_side_total_vol`, `is_simtrade`, `is_odd_lot`. **No trade-side classification** (buy/sell).
- **Latency profile** (Shioaji P95): submit=36ms, modify=43ms, cancel=47ms. Internal pipeline ~250us.
- **TAIFEX TXFD6**: ~125ms median inter-tick. No maker rebates. 2.0 bps sell tax.

---

## Candidate A: Liquidity Withdrawal Anticipation (LWA)

### 1. Latency Feasibility

The survey claims a 1-5 second anticipation window. At 125ms median inter-tick, 1 second = ~8 ticks, 5 seconds = ~40 ticks. With 36ms submit latency, if the signal persists for even 2-3 ticks (250-375ms) after detection, there is enough time to act.

**Assessment**: Feasible IF the signal half-life is validated at >= 500ms on TXFD6 data. The survey acknowledges this risk. Must be validated in Stage 2 with actual decay analysis.

### 2. Data Availability

LWA requires reconstructing cancellations and additions from consecutive BidAskEvent snapshots. This is the critical technical question.

**Reconstruction analysis**:
- We receive aggregate `(price, qty)` at each level per snapshot. We do NOT receive individual order-level events (adds, cancels, modifies).
- Between two consecutive BidAskEvents, at a given price level:
  - `qty_decrease` could be: (a) cancellation, (b) execution/fill, or (c) level shifting (price moved).
  - `qty_increase` could be: (a) new addition, or (b) level shifting.
- **Critical ambiguity**: We cannot distinguish cancellations from executions. A qty decrease at L1 bid could be a cancel (informed withdrawal) or a fill (trade happened). Without trade-side classification in TickEvent, we cannot resolve this.
- **Mitigation**: Can use TickEvent volume to estimate fills. If `tick.volume > 0` coincides with bid L1 qty decrease, some of the decrease is likely fill, not cancel. But this is approximate — multiple cancels + a fill can occur within the same snapshot interval.
- **Price-keyed tracking**: Required. Level indices shift when prices move. Implementation must use price as the key, not level index.

**Assessment**: CONDITIONAL. Reconstruction is feasible but noisy. The cancel/fill ambiguity is real and will degrade signal quality. The survey underestimates this complexity ("~50 lines Python" is optimistic for robust price-keyed diff logic with fill deconvolution). Estimate: ~150-200 lines for a correct implementation.

### 3. FeatureEngine Integration

- Requires access to full L2-L5 `bids`/`asks` arrays from BidAskEvent. Currently, FeatureEngine only extracts L1 via `_extract_l1_qty()`.
- Must store previous snapshot per symbol (5 bid levels + 5 ask levels = 10 price/qty pairs). This is ~80 bytes per symbol — negligible memory.
- New feature indices: at minimum 2 (lwi_bid, lwi_ask), preferably 4 (lwi_bid_raw, lwi_ask_raw, lwi_bid_zscore, lwi_ask_zscore). The z-score requires a rolling window state (~50 values).
- Compute cost per tick: iterate L1-L5 (10 levels), diff against previous, compute ratio. Vectorizable with numpy. Estimated: 2-5us Python, <1us Rust. Acceptable.
- Hot-path impact: Moderate. Must access `event.bids` and `event.asks` arrays (currently only accessed for L1). No new external data needed.

**Assessment**: 4 new feature indices (18-21). Requires expanding FeatureEngine to consume L2-L5 from BidAskEvent, which is a meaningful change to `_compute_values()`. No new config params needed beyond feature set version bump.

### 4. Config Compatibility

- No new environment variables required.
- No new config files required.
- Risk limits: LWA as a conditional gate does not introduce new order types or risk dimensions. No risk config changes needed.

**Assessment**: Config drift = 0. PASS.

### 5. Reconstruction Feasibility (Specific Check)

As detailed in section 2:
- **Can we reconstruct cancels vs adds?** Partially. We can compute net depth change per price level. We cannot cleanly separate cancels from fills without order-level data.
- **Is this a blocker?** Not necessarily. Wang (2025) uses a similar aggregate approach. The LWI = (depth_decrease) / (standing_depth + depth_increase) can work with aggregate changes, treating fills and cancels as equivalent "withdrawals." This is theoretically defensible — both reduce available liquidity.
- **Price-keyed tracking**: Required. Level indices shift when prices move. Implementation must use price as the key, not level index.

### Verdict: CONDITIONAL APPROVE

Conditions:
1. Stage 2 must validate signal half-life >= 500ms on TXFD6 data.
2. Implementation must use price-keyed diffs (not level-index diffs) to handle price shifts correctly.
3. Accept that cancel/fill cannot be separated — use combined "withdrawal" metric. Document this limitation in the alpha manifest.

---

## Candidate B: Regime-Conditional OFI

### 1. Latency Feasibility

Regime classification (depth/spread/volatility bins) changes slowly — on the order of seconds to minutes. OFI signal itself is already computed per tick. The conditional gating adds negligible compute. Once in a favorable regime, the existing OFI-to-order path applies.

**Assessment**: No latency concerns. Regime state is slow-moving relative to our 36ms submit latency. PASS.

### 2. Data Availability

All required fields exist today:
- OFI: computed as `ofi_l1_raw` (index 11), `ofi_l1_cum` (12), `ofi_l1_ema8` (13)
- Spread: `spread_scaled` (index 3), `spread_ema8_scaled` (14)
- Depth: `bid_depth` (4), `ask_depth` (5), `depth_imbalance_ppm` (6)
- Imbalance EMA: `depth_imbalance_ema8_ppm` (15)
- Volatility: Not directly available as a feature. Would need to be computed from price returns. Can use spread_ema8 as a volatility proxy, or add a realized-vol feature.

**Assessment**: 95% available. Volatility regime requires either (a) a new rolling realized-vol feature, or (b) using spread_ema8 as a proxy. The proxy approach requires zero new features. The proper approach adds 1 feature (realized_vol_ema or return_var_ema).

### 3. FeatureEngine Integration

- **Zero new features** if using existing spread/depth as regime dimensions.
- **1 new feature** if adding a proper volatility metric (index 16 or 18 depending on v2 status).
- Regime classification itself is strategy logic, not FeatureEngine logic — it belongs in the strategy layer. No FeatureEngine changes required for the core approach.
- Compute cost: trivial (threshold comparisons on existing features). <100ns.

**Assessment**: Minimal FeatureEngine impact. Primarily a strategy-layer change. PASS.

### 4. Config Compatibility

- No new environment variables needed.
- No new config files needed.
- Risk limits unchanged — OFI already generates OrderIntents; this just gates when they fire.

**Assessment**: Config drift = 0. PASS.

### 5. OOS Regime Collapse Risk

The survey identifies this correctly. Regime boundaries calibrated in-sample may not hold OOS. However, this is a research risk, not an execution/tradability blocker.

**Assessment**: Research concern, not execution concern.

### Verdict: APPROVE

Lowest execution risk of all three candidates. Uses existing features, requires no FeatureEngine changes for the baseline approach, and regime classification is naturally slow-moving relative to our latency constraints. The only caveat is that if a proper volatility feature is desired, it adds one feature index to the engine.

---

## Candidate C: LOB Active Depth Momentum

### 1. Latency Feasibility

KE/momentum is a state metric computed from the current snapshot — not a transient event signal. It changes at the rate of BidAskEvent updates (~125ms). No latency concern for computation. As a feature (not a standalone strategy), latency-to-act is not directly relevant.

**Assessment**: PASS. This is a feature, not a strategy — latency feasibility is N/A.

### 2. Data Availability

Requires full L1-L5 bid/ask price and quantity arrays. Available in `BidAskEvent.bids` and `BidAskEvent.asks` (shape (N,2), col 0 = price, col 1 = qty).

Requires mid-price for distance calculation. Available from LOBStatsEvent `mid_price_x2`.

**Assessment**: All fields available. No data gaps. PASS.

### 3. FeatureEngine Integration

- Requires access to full `bids`/`asks` arrays from BidAskEvent — same requirement as Candidate A. Currently not consumed beyond L1.
- **New feature indices**: Survey proposes 3 (lob_ke_momentum, lob_active_depth_ratio, lob_gravity_center) at indices 18-20.
- KE computation: iterate L1-L5, compute `qty * distance^2`. With numpy: `np.sum(bids[:,1] * (mid - bids[:,0])**2)`. Vectorized, estimated 1-3us Python.
- **Hot-path concern**: The `distance^2` computation involves `(mid_price_x2/2 - price)^2`. With scaled integers (x10000), squaring produces very large numbers. For TXFD6 at ~20000 points, L5 distance might be ~20 ticks = 200000 in scaled units. Squared = 4e10. Multiplied by qty (say 100) = 4e12. Sum over 5 levels: up to 2e13. This fits in int64 (max 9.2e18) but is getting large. Must verify no overflow with extreme values.
- **Normalization**: KE_bid and KE_ask must be normalized before taking the ratio. The formula `(KE_bid - KE_ask) / (KE_bid + KE_ask)` produces a float in [-1, 1]. To keep integer semantics, scale to PPM: `(KE_bid - KE_ask) * 1_000_000 / (KE_bid + KE_ask)`.
- **Active depth detection**: The survey mentions "rolling correlation of level-specific depth changes with price" — this is significantly more complex and requires multi-tick state per level. Defer this to a later phase.

**Assessment**: Core KE/momentum is feasible. 2-3 new features. Requires same L2-L5 access expansion as Candidate A. Integer overflow risk is manageable but must be tested. "Active depth" sub-feature is more complex than described — recommend deferring it.

### 4. Config Compatibility

- No new environment variables.
- No new config files.
- No risk limit implications (pure feature, not strategy).

**Assessment**: Config drift = 0. PASS.

### Verdict: APPROVE

Clean feature addition. All data is available. Compute cost is low and vectorizable. The only engineering concern is the FeatureEngine L2-L5 access expansion (shared with Candidate A) and integer overflow testing for the KE computation. Recommend implementing only `lob_ke_momentum` and `lob_gravity_center` initially (2 features), deferring `active_depth_ratio` which requires more complex multi-tick correlation state.

---

## Cross-Candidate Analysis

### Shared Prerequisite: L2-L5 Access in FeatureEngine

Both Candidates A and C require expanding `FeatureEngine._compute_values()` to consume L2-L5 data from `BidAskEvent.bids`/`asks` arrays. This is currently not done — only L1 is extracted via `_extract_l1_qty()`. This is a **shared prerequisite** that should be implemented first as a foundation.

Estimated work: Modify `_compute_values()` to extract and store L2-L5 price/qty pairs. Add previous-snapshot storage for diff computation (Candidate A needs this). Approximately 50-80 lines of new code in `engine.py`.

### FeatureEngine v2 Status Correction

The survey states "FeatureEngine v2: 18 features" as existing infrastructure. This is **incorrect**. Only `lob_shared_v1` (16 features) exists in code. The `lob_shared_v2` feature set with ISS [16] and MLDM [17] has not been implemented. Any feature indexing for new candidates should start at index 16, not 18.

If ISS and MLDM are to be implemented alongside Round 15 features, the total new feature count would be:
- ISS: 1 feature (index 16)
- MLDM: 1 feature (index 17)
- Candidate C: 2 features (indices 18-19)
- Candidate A: 4 features (indices 20-23)
- Total: 8 new features, bringing the engine from 16 to 24 features

This is a significant expansion. The `changed_mask` and `warmup_ready_mask` are int bitmasks — at 24 features they still fit in a 32-bit int, so no structural issue. But the compute budget per tick increases meaningfully.

### Implementation Order Recommendation

The survey's recommended order (C -> A -> B) is correct from an execution perspective:
1. **Phase 0** (prerequisite): Expand FeatureEngine L2-L5 access
2. **Phase 1**: Candidate C (LOB KE/momentum) — pure feature, lowest risk
3. **Phase 2**: Candidate A (LWA) — depends on Phase 0 + snapshot diff infrastructure
4. **Phase 3**: Candidate B (Regime-Conditional OFI) — strategy-layer, depends on validated features

---

## Summary Table

| Candidate | Verdict | Key Concern | Config Drift | New Features | Data Available |
|-----------|---------|-------------|-------------|-------------|----------------|
| A: LWA | CONDITIONAL APPROVE | Cancel/fill ambiguity in snapshot diffs; signal half-life must be validated >= 500ms | 0 | 4 (indices 16-19 or 20-23) | Yes (BidAskEvent L1-L5) |
| B: Regime-Conditional OFI | APPROVE | Regime frequency may be low; OOS stability | 0 | 0-1 | Yes (all existing) |
| C: LOB Active Depth Momentum | APPROVE | Integer overflow in KE computation; defer active_depth sub-feature | 0 | 2-3 (defer active_depth) | Yes (BidAskEvent L1-L5) |

### Critical Corrections for the Survey
1. **FeatureEngine v2 does not exist** in codebase. Only `lob_shared_v1` (16 features) is implemented. Feature indices should start at 16, not 18.
2. **Candidate A complexity is underestimated**: Price-keyed diff logic with fill deconvolution is ~150-200 lines, not ~50 lines.
3. **L2-L5 access is a shared prerequisite** not called out in the survey — FeatureEngine currently only consumes L1 from BidAskEvent.

---

*Review complete. No candidates rejected. All three are technically feasible with the caveats noted above.*
