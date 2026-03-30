# Round 22 Stage 1: Execution Review -- LOB Slope & Convexity

**Reviewer**: Execution Reviewer
**Date**: 2026-03-28
**Survey reviewed**: `round22_stage1_literature_survey.md`

---

## 1. Overall Assessment: CONDITIONAL APPROVE

Approve Candidate A (DWSA) and Candidate C (DCI) for Gate Zero diagnostic only. REJECT Candidate B (RC-OFI) outright -- signal half-life risk is too severe given our latency profile. The survey is well-structured and correctly identifies the key risks. However, there are two infrastructure claims that are factually incorrect, and the cost model for TXFD6 needs correction.

---

## 2. Latency Compatibility (Per Candidate)

**Reference**: Shioaji P95 latency profile (v2026-03-04):
- Submit: 36 ms
- Modify: 43 ms
- Cancel: 47 ms
- Internal pipeline: ~250 us

### Candidate A (DWSA) -- PASS
- Claimed horizon: 5-30 seconds.
- At 36ms RTT, minimum viable half-life is ~200ms (need at least 5x RTT headroom for signal capture + decision + execution). The 5-30s range provides 100x+ margin.
- **Verdict**: Latency is not a constraint. Safe.

### Candidate B (RC-OFI) -- REJECT
- Claimed horizon: 1-15 seconds.
- The survey states "36ms RTT is compatible if the signal half-life is >1s." This is **incorrect**. Minimum viable half-life = ~200ms for the signal to exist, but for a profitable trade you need: signal detection (~50ms internal) + order submission (36ms P95) + queue priority (unknown, likely 50-200ms on TAIFEX) + fill confirmation (36ms) + exit (36ms+). **Total execution round-trip is 200-500ms at P95.** A 1s half-life signal with 500ms total execution latency leaves only ~500ms of alpha, which is marginal.
- The real problem: RC-OFI is `OFI/depth`. On TMFD6 (large-tick, median spread = 3 pts), L1 depth is the dominant contributor. The depth denominator varies slowly, so this reduces to a noisy re-scaling of OFI. The survey correctly identifies this in the "Key risk" section but still rates it as DEFER rather than REJECT.
- At the 5s horizon target, the survey states IC > 0.100 is needed. This is an extremely aggressive target that no TXFD6/TMFD6 microstructure signal has achieved in any prior round. Combined with the half-life risk, this candidate is DOA.
- **Verdict**: REJECT. Half-life risk + IC target + large-tick depth stability = triple kill.

### Candidate C (DCI) -- PASS (conditional)
- Claimed horizon: 10-60 seconds.
- 300x+ margin over RTT. No latency concern.
- **Verdict**: Latency is not a constraint. Safe.

---

## 3. Feature Engine Integration Feasibility

### Current State
- FeatureEngine v2 (`lob_shared_v2`) has 21 features (indices 0-20).
- Default is `lob_shared_v2` (schema_version=2).
- Features [19] (`impact_surprise_x1000`) and [20] (`deep_depth_momentum_x1000`) are provisional with 30-day kill gate.
- The MLDM feature [20] already extracts L2-L5 quantities from the `BidAskEvent.bids`/`asks` arrays in `engine.py` (lines 650-674). This is the exact same data path that DWSA and DCI would need.

### Integration Path for New Features
- **dwsa_l3_x1000**: Would be slot [21]. Needs L1-L3 bid/ask volumes. The BidAskEvent already carries these (shape (N,2) arrays). Computation is trivial (3 additions + 2 divisions). No new data dependencies.
- **rc_ofi_x1000**: REJECTED, not applicable.
- **depth_convexity_x1000**: Would be slot [22]. Needs L1-L3 bid/ask volumes. Same data source as DWSA. Two subtractions per side.

### Schema Versioning Concern
Adding features [21] and [22] requires bumping to `lob_shared_v3` (schema_version=3). This triggers:
1. All alpha manifests declaring `feature_set_version` must be updated.
2. Existing strategies consuming the feature tuple will see a longer tuple (backward compatible since they index by position, but must not assume fixed tuple length).
3. The Rust `LobFeatureKernelV1` backend does NOT compute ISS/MLDM (falls back to Python). New features would similarly need Python fallback.

### Verdict: FEASIBLE
No blocking issues. The existing `_compute_mldm_deep_depth_momentum` method in `engine.py` provides a direct template for extracting L2-L3 volumes from `BidAskEvent`. Implementation effort: ~50 LOC per feature in `engine.py` + registry update.

---

## 4. Data Pipeline Gaps

### CRITICAL FINDING: L5 Data Flows in Live Pipeline

The survey implicitly treats L5 data as "historical only" (ClickHouse export). This is **incorrect**. Let me clarify:

1. **Live pipeline**: Shioaji provides 5-level bid/ask data. The normalizer (`feed_adapter/normalizer.py`) processes all 5 levels and constructs `BidAskEvent` with `bids`/`asks` arrays of shape (5,2). This flows through LOBEngine into the FeatureEngine via `process_lob_update(event, stats)`.

2. **FeatureEngine already consumes L2-L5**: The MLDM feature [20] extracts L2-L5 quantities from the live BidAskEvent (engine.py lines 650-674). This means the proposed DWSA and DCI features can be computed on the live pipeline without any infrastructure changes.

3. **ClickHouse historical data**: L5 depth data IS stored in ClickHouse (`hft.market_data`). The `ch_batch_export.py` script supports `--formats l1,l2` but does **NOT** support `--formats l5`. The survey claims `--formats l5` is available (Section 4.1: "Export via `ch_batch_export.py --formats l5`"). This is factually wrong. The script's `--formats` argument only accepts `l1` and `l2` (line 591: `help="Comma-separated: l1 (research .npy), l2 (hftbacktest .npz)"`).

### Infrastructure Gap: L5 Research Export
- For Gate Zero diagnostic, we need L5 depth data in research-friendly numpy format.
- Option 1: Add `l5` format to `ch_batch_export.py` (~100 LOC, extracting bid/ask prices and volumes at levels 1-5 from ClickHouse).
- Option 2: Use existing L2 (hftbacktest) format which already contains full book snapshots with depth levels.
- Option 3: Query ClickHouse directly with SQL for the diagnostic (fastest path, no tooling change needed).

### Verdict: NOT A BLOCKER
L5 data is available in both live and historical pipelines. The `--formats l5` claim in the survey is wrong, but this is a minor tooling gap (can be worked around with direct SQL or by extending the export script). The key point is that **live pipeline integration requires ZERO infrastructure changes** because BidAskEvent already carries L5 data and the FeatureEngine already knows how to extract it.

---

## 5. Cost Model Verification

### TMFD6 (Mini Taiwan Futures)
- 1 point = 10 NTD (confirmed per `feedback_mini_taiex_point_value.md`)
- RT cost: tax (2.0 bps sell) + commission (2 x ~13 NTD) = ~3.92 pts
- Survey states "~4 pts RT cost" -- **CORRECT**

### TXFD6 (Taiwan Futures)
- Survey states "~4 pts RT cost" -- this needs clarification.
- TXFD6: 1 point = 200 NTD. Tax = 2.0 bps sell, commission = 2 x ~100 NTD.
- At TXFD6 price ~23000: sell tax = 23000 * 200 * 0.0002 / 200 = 0.92 pts. Commission = 200/200 = 1 pt per side = 2 pts RT. Total ~2.92 pts.
- The survey's "~4 pts" for TXFD6 is conservative but acceptable for kill-gate purposes.

### IC Breakeven Calculations
From R17 analysis:
- At 10s horizon: IC breakeven ~0.050 (TMFD6)
- At 30s horizon: IC breakeven ~0.030 (TMFD6)
- At 60s horizon: IC breakeven ~0.020 (TMFD6)

Survey's stated thresholds:
- DWSA at 10s: IC > 0.050 -- **correct**
- DWSA at 30s: IC > 0.030 -- **correct**
- DCI at 30s: IC > 0.030 -- **correct**
- DCI at 60s: IC > 0.020 -- **correct**
- RC-OFI at 5s: IC > 0.100 -- **correct and correctly identified as aggressive**

### Verdict: PASS
Cost model is accurate. IC breakeven thresholds are correctly derived.

---

## 6. Infrastructure Readiness

### Data Export (`ch_batch_export.py`)
- **`--formats l5` does NOT exist.** Survey claim is incorrect (Section 4.1 and 4.3).
- Workaround for Gate Zero: direct ClickHouse SQL query to extract L1-L5 bid/ask volumes. No tooling blocker.

### L5 Data Coverage
- Shioaji provides 5-level data for futures contracts (TXFD6, TMFD6).
- ClickHouse stores all 5 levels in the bid/ask arrays.
- Data coverage: depends on `market_data` table dates. The R20 survey confirmed L5 data availability. Exact date range should be verified at Gate Zero.

### L5 Data Quality Concern
- R15 found "L3-L5 add noise" on TXFD6. This is NOT a data quality issue -- it may be a structural property of TXFD6's order book (thin deeper levels).
- Gate Zero K4 (L2/L3 volume existence rate) will resolve this. If < 70% of ticks have non-zero L2+L3 volume, all candidates are DOA.

### FeatureEngine Integration
- Adding 2 features (DWSA, DCI) to `lob_shared_v3` is straightforward.
- Template exists: MLDM feature extraction code in `engine.py`.
- Kernel state additions needed in `_LobKernelState` for any rolling/EMA components (DWSA is stateless per-tick; DCI is stateless per-tick).
- Actually, both DWSA and DCI as described are **pure snapshot features** (no state). This is simpler than MLDM. They can be computed directly from the current BidAskEvent without any kernel state.

### Verdict: READY (with minor workaround for research export)

---

## 7. Recommendations

### Immediate Next Steps (Gate Zero Diagnostic)

1. **Run L2/L3 volume presence diagnostic** on TXFD6 and TMFD6 via direct ClickHouse SQL:
   ```sql
   SELECT
     count(*) AS total_ticks,
     countIf(bid_qty_l2 > 0 AND ask_qty_l2 > 0) AS l2_present,
     countIf(bid_qty_l3 > 0 AND ask_qty_l3 > 0) AS l3_present,
     avg(bid_qty_l2) AS avg_bid_l2,
     avg(bid_qty_l3) AS avg_bid_l3,
     stddevSamp(bid_qty_l2) / avg(bid_qty_l2) AS cv_bid_l2,
     stddevSamp(bid_qty_l3) / avg(bid_qty_l3) AS cv_bid_l3
   FROM hft.market_data
   WHERE symbol = 'TXFD6'
   ```
   If L2/L3 volume presence < 70%, STOP. All candidates are killed.

2. **Compute DWSA vs depth_imbalance correlation** on same data. If r > 0.60, DWSA has no incremental value.

3. **Check depth profile shape**: Is TXFD6's average book hump-shaped or monotonically decreasing? If monotonic, DCI (second derivative) is degenerate.

### Architecture Decisions

4. **Do NOT create `lob_shared_v3` until Gate Zero passes.** Adding features to the registry is cheap but bumping schema version has downstream implications. Wait until at least one candidate survives Gate Zero.

5. **Implementation path if a candidate passes**: Add compute function to `FeatureEngine._compute_values()` alongside MLDM code. Both DWSA and DCI are stateless per-tick, so no `_LobKernelState` additions needed. ~30 LOC each.

6. **Strategy integration**: DWSA and DCI, if validated, would most likely serve as **CBS filter features** (similar to the R17 2330 lead-lag filter) rather than standalone strategies. Given the R16 structural finding that "no microstructure alpha on TMFD6 front-month at L1 level with 4.0 pts RT cost," these signals are unlikely to justify standalone entry/exit. As CBS filters, they can be consumed from the feature tuple by index, requiring zero strategy framework changes.

7. **Max drawdown recommendation**: For any shadow deployment of slope/convexity signals, use the same risk limits as CBS: max_dd = 500 pts (TMFD6), max_position = 1 lot. These are pure L1-L3 microstructure signals with no demonstrated edge yet.

### Corrections to Survey

8. **Fix `--formats l5` claim**: Section 4.1 states "Export via `ch_batch_export.py --formats l5`". This format does not exist. Replace with "Query ClickHouse directly or extend `ch_batch_export.py` to support `l5` format."

9. **Fix RC-OFI assessment**: Change from DEFER to REJECT. The combination of (a) marginal half-life vs execution latency, (b) IC > 0.100 target at 5s, and (c) large-tick depth stability on TMFD6 makes this candidate unviable. No prior round has achieved IC > 0.100 on any horizon for any TAIFEX signal.

### Prior Round Cross-Reference

10. **Key precedent risk**: R15 tested depth-based features (LOB KE, gravity center) and found "L1 dominates; L3-L5 add noise." R20 tested "depth shape" and killed it for N=20 insufficient observations. The survey correctly identifies that the resolution may be timescale (R15 tested at tick-level, Bechler & Ludkovski used volume-bucketed meso-scale). Gate Zero must specifically test at 10-60s horizons, not per-tick.

11. **The survey's structural hypothesis is sound**: If deeper LOB shape only matters on the meso-scale (10-60s), and our prior work tested at tick-level, there is genuinely unexplored territory here. The key Gate Zero test is whether L2-L3 information provides incremental IC at the 10-60s horizon after controlling for L1 depth_imbalance.

---

## 8. Summary Table

| Candidate | Latency | Feature Engine | Data Pipeline | Cost Model | Verdict |
|-----------|---------|----------------|---------------|------------|---------|
| A (DWSA) | PASS | FEASIBLE | L5 available live + historical | CORRECT | CONDITIONAL GO for Gate Zero |
| B (RC-OFI) | FAIL (marginal) | N/A | N/A | IC target unrealistic | **REJECT** |
| C (DCI) | PASS | FEASIBLE | L5 available, L3 volume TBD | CORRECT | CONDITIONAL GO for Gate Zero |

**Gate Zero is the binding constraint.** If L2/L3 volumes are sparse on TXFD6/TMFD6, none of this matters.
