# T2: Kill Checklist Review — All 3 Candidates

## Summary

| Candidate | Verdict | T1 FAIL | T2 FAIL | Key Reason |
|-----------|---------|---------|---------|------------|
| C1: Cont-Kukanov Fill Probability | **REJECT** | 2 | 4 | Non-binding for R47 (spread>=4 → limit always correct); paper assumes maker rebates |
| C2: IS TCA Per-Fill Decomposition | **APPROVE** | 0 | 0 | Verified gap (5/9 metrics = 0), ~60 lines, zero hot-path risk |
| C3: Latency Adverse Selection | **REJECT** | 5 | 6 | Repackaged R51 C1/C3, contradicts R47 structural properties, RTT infeasible |

## C1: Cont-Kukanov — REJECT

### Tier 1
- H1 FAIL: R47 operates at spread>=4 where limit is always correct. Threshold 1.5 is non-binding.
- H2 FAIL: At spread=4 (39.5% of time), limit saves 2 pts unambiguously. Model only helps at spread=2-3 where R47 doesn't trade.
- H3 PASS: Not an R51 killed direction (order-type selection, not adverse fill reduction).
- H4 PASS: ClickHouse has required LOB + fills data.
- H5 PASS: Offline calibration + same-path decision. No latency dependency.
- H6 PASS (WARN): Could reduce L1 quoting frequency if sometimes decides MARKET at spread>=4.

### Tier 2
- S1 FAIL: Paper assumes US equity maker rebates (Section 2.1).
- S3 FAIL: ~20-50 fills/day insufficient for 0.5-sigma effect size.
- S5 FAIL: Paper assumes (a) rebates, (b) co-location, (c) sub-ms cancel.
- S6 FAIL: R47 structural property: "all gates disabled is already optimal."

## C2: IS TCA — APPROVE

### Tier 1: All PASS
- H1: Verified gap — `_DAILY_QUERY` doesn't SELECT decision_price/arrival_price. 5/9 metrics hardcoded 0.
- H4: `hft.fills` has decision_price (Int64), arrival_price (Int64) since migration 20260327_002.
- H6: Pure observability, zero R47 interference.

### Tier 2: All PASS
- S1: IS decomposition is universal (Perold 1988), no market structure dependency.
- S6: Currently 5/9 TCA metrics useless. Without this, no future execution improvement is measurable.

## C3: Latency Placement — REJECT

### Tier 1
- H1 FAIL: 0.5 bps → 1.05 pts/trade → ~31.5 pts/day = doubling R47 PnL. Implausible transfer from sub-ms to 30-50ms RTT.
- H2 FAIL: R51 C3b-B tested cancel+reinsert on CK direct: 0/12 days improved, PnL/fill -0.052 worse.
- H3 FAIL: Repackaged adverse selection control = R51 C1/C3 killed mechanism.
- H5 FAIL: 30-50ms RTT consumes 24-40% of median inter-tick interval (125ms). Too slow.
- H6 FAIL: R47 property #6: "reducing L1 quoting during volatility destroys edge."

### Tier 2: All 6 FAIL
- S1: Papers assume co-location, maker rebates, sub-ms reaction.
- S2: Cannot validate offline (queue position dynamics change with cancel/reinsert).
- S4: R47's edge EXISTS in ADVERSE regimes.
- S6: RegimeClassifier already feeds ExecutionOptimizer (binary gate captures directional effect).
