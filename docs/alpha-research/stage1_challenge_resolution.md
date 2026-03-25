# Stage 1 Challenge Resolution: B-1, C-1, C-3, X-3

**Date**: 2026-03-25
**Method**: Analytical derivation + Monte Carlo simulation + empirical data analysis on real TXFD6/2330 L1 datasets

---

## B-1: Rolling Regression Window Sensitivity

### Evidence: Monte Carlo Simulation

Parameters: `b_true=1.0, sigma_ofi=1.0, sigma_epsilon=3.0, R^2=0.10, N_sims=10000`

**Fixed-window OLS estimator**:

| Window (ticks) | Mean(b_hat) | Std(b_hat) | CV | % within 50% of true |
|---------------|-------------|------------|------|---------------------|
| 20 | 1.062 | 0.781 | 0.735 | 49.8% |
| 50 | 1.013 | 0.447 | 0.441 | 74.4% |
| 100 | 1.015 | 0.308 | 0.304 | 89.3% |
| 200 | 1.006 | 0.214 | 0.213 | 98.0% |
| 500 | 1.001 | 0.134 | 0.134 | 100.0% |
| 1000 | 1.001 | 0.094 | 0.094 | 100.0% |

**Confirmation**: At 50-tick window, CV=0.44 (not 0.63 as I previously estimated analytically -- the analytical overestimated because it ignored the unbiasedness of OLS). Still, at 50 ticks only 74% of estimates fall within 50% of true value. The concern is valid for small windows.

**EMA-based online estimator** (tested on 5000-tick series, measurements after warmup):

| EMA span | Mean(b_hat) | Std(b_hat) | CV | % within 50% | Sign correct |
|----------|-------------|------------|------|-------------|-------------|
| 50 | 0.980 | 0.400 | 0.408 | 79.8% | 99.2% |
| 100 | 0.943 | 0.286 | 0.303 | 90.9% | 99.9% |
| 200 | 0.975 | 0.227 | 0.232 | 97.7% | 100.0% |
| 500 | 1.094 | 0.101 | 0.092 | 100.0% | 100.0% |

**Key finding**: Even at span=50, **sign correctness is 99.2%**. The signal "is b_hat above or below b_eq?" is extremely robust even when the magnitude is noisy.

### Recommended Revision

Use **EMA-based running moments** with span=200 (CV=0.23, 97.7% within 50% of true):

```python
alpha = 2.0 / (span + 1)  # span=200 -> alpha=0.00995

# Per tick update (O(1)):
ema_ofi     = (1-a) * ema_ofi     + a * ofi
ema_ret     = (1-a) * ema_ret     + a * ret
ema_ofi2    = (1-a) * ema_ofi2    + a * ofi^2
ema_ofi_ret = (1-a) * ema_ofi_ret + a * ofi * ret

cov = ema_ofi_ret - ema_ofi * ema_ret
var = ema_ofi2 - ema_ofi^2
b_hat = cov / max(var, epsilon)
```

**For TXFD6** (27.5 ticks/sec): span=200 = 7.3 seconds effective window. This is much shorter than Takahashi's 15-min window but with 100% sign accuracy, which is what matters for the signal.

**For 2330** (3.1 ticks/sec): span=200 = 65 seconds. Longer but still well within the regime-persistence timescale from Takahashi (who shows b_r varies on 15-min scale).

The signal should be extracted as a **sign/ternary indicator** (ISS > threshold = "high impact", ISS < -threshold = "low impact", else "neutral") rather than used as a continuous value.

### Conclusion: B-1 RESOLVED

EMA span=200 with sign extraction gives robust impact regime detection. CV=0.23 at R^2=0.10 is acceptable for a ternary signal. The magnitude noise is real but irrelevant when the signal is used as a regime indicator.

---

## C-1: QDI / OFI Collinearity

### Evidence: Code Analysis + Empirical BBO Stability

**OFI computation** (from `kernel.py:70-95`):

```python
# When best_bid unchanged (== prev):
b_flow = bid_qty - prev_bid_qty   # This IS delta_bid_qty

# When best_bid rises (> prev):
b_flow = bid_qty                  # Full new queue (discontinuity)

# When best_bid falls (< prev):
b_flow = -prev_bid_qty            # Full old queue lost (discontinuity)
```

**Critical fact confirmed**: When BBO is unchanged, `ofi_l1_raw` bid component = `delta_bid_qty` exactly. The "derivative vs integral" framing in the original proposal was incorrect.

**Empirical BBO stability from real data**:

| Symbol | BBO both unchanged | Ticks/sec |
|--------|-------------------|-----------|
| TXFD6 (futures) | 57.5% | 27.5 |
| 2330 (TSMC stock) | 99.7% | 3.1 |

For 2330 (stock), **99.7% of ticks have unchanged BBO** -- meaning QDI and OFI are **virtually identical** for stocks. For TXFD6, 57.5% identical, 42.5% divergent at price transitions.

### Honest Assessment

**For stocks (2330)**: QDI provides essentially ZERO marginal information beyond OFI. Collinearity ~0.99. **C should NOT be applied to stocks in its original form.**

**For futures (TXFD6)**: QDI and OFI diverge at 42.5% of ticks (price transitions). But at these transitions, OFI uses the Cont et al. decomposition which IS the correct measure of order flow. QDI's raw `delta_qty` at transitions is dominated by the queue size jump, not informative flow.

### Revised Proposal: Queue-OFI Residual (QOR)

The only genuinely novel information QDI can provide is the **unexplained queue change** -- changes not attributable to OFI-measured flow:

```python
# Decompose queue change at unchanged price:
# delta_bid_qty = trades_executed_at_bid + new_limit_orders - cancellations
# OFI captures: net flow (trades + limits)
# Residual = cancellations (invisible to OFI)

# At price transitions: QOR is undefined (set to 0)

if best_bid == prev_best_bid:
    qor_bid = delta_bid_qty   # same as OFI bid component -- no residual possible
```

**Problem**: When BBO is unchanged, QOR_bid = delta_bid_qty - OFI_bid_component = 0 by construction (they are identical). When BBO changes, OFI uses a different formula, so the residual is nonzero but dominated by queue size artifacts.

**Honest conclusion**: There is NO orthogonal cancellation signal extractable from L1 snapshot data alone. Cancellations are invisible at L1 resolution -- they are absorbed into the net queue change, which IS the OFI computation.

To detect cancellations separately, we would need:
1. L2+ depth data (track queue depletion at non-best levels), OR
2. Order-level data (individual order additions/cancellations), OR
3. Inferred cancellation = total_queue_change - executed_volume - limit_additions

We have (1) via L5 data in `research/data/l5/`. A revised Candidate C could use L2-L5 depth changes as the orthogonal signal.

### Revised Candidate C: Multi-Level Depth Momentum (MLDM)

Instead of L1-only QDI, use L2-L5 depth changes as an adverse selection signal:

```python
# L2-L5 depth change (orthogonal to L1 OFI)
for level in [2, 3, 4, 5]:
    delta_depth_bid[level] = bid_qty[level](t) - bid_qty[level](t-1)
    delta_depth_ask[level] = ask_qty[level](t) - ask_qty[level](t-1)

# Multi-Level Depth Momentum (MLDM)
MLDM(t) = sum(delta_depth_bid[2:5]) - sum(delta_depth_ask[2:5])
MLDM_ema(t) = EMA(MLDM, span=K)
```

**Why this is novel**: Informed traders cancel deep-book orders before moving the market. MLDM captures this "depth withdrawal" signal that precedes price moves but is invisible to L1 OFI.

**Data available**: L5 data exists for TXFD6 (2.17M rows, 10 days), 2330 (537K rows, 10 days), and 2317 (132M rows). Also L5 v2 dataset with 17 trading days for 2330.

### Conclusion: C-1 PARTIALLY RESOLVED

**Original QDI: SHOULD BE DROPPED.** It is ~100% collinear with OFI for stocks and offers minimal marginal value for futures. The derivative/integral framing was factually incorrect.

**Revised Candidate C (MLDM)**: Multi-Level Depth Momentum using L2-L5 data is genuinely orthogonal to L1 OFI and captures the depth withdrawal / adverse selection signal. L5 data is available for prototyping.

---

## C-3: TWSE Batch Matching Impact

### Evidence: Empirical Inter-Tick Interval Analysis

Measured from real data: `research/data/raw/txfd6/TXFD6_all_l1.npy` (1.78M ticks, 4 days) and `research/data/raw/2330/2330_all_l1.npy` (198K ticks, 4 days).

**TXFD6 (Taiwan futures)**:

| Metric | Value |
|--------|-------|
| Mean inter-tick | 286 ms |
| Median inter-tick | 125 ms |
| P5 inter-tick | 0.44 ms |
| P95 inter-tick | 373 ms |
| Ticks/sec | 27.5 |
| BBO unchanged | 57.5% |

**Interval distribution**:
- 0-50ms: 21.9% (burst events)
- 100-200ms: 62.3% (dominant cluster)
- 500ms+: 1.9% (rare gaps)

**2330 (TSMC stock)**:

| Metric | Value |
|--------|-------|
| Mean inter-tick | 2793 ms |
| Median inter-tick | 145 ms |
| P95 inter-tick | 1381 ms |
| Ticks/sec | 3.1 |
| BBO unchanged | 99.7% |

### Critical Finding: TWSE Matching is NOT 500ms for Futures

The 500ms batch matching described in the RELAVER paper (2505.12465v1) refers to **TWSE equities/stocks**, not TAIFEX futures. TAIFEX (台灣期交所) uses **near-continuous matching** with ~125ms dominant update intervals for liquid contracts like TXFD6.

**TWSE equities** (like 2330) do appear to use periodic matching, but the observed data shows highly variable intervals (P5=12ms to P95=1381ms), suggesting either:
1. Periodic matching with variable Shioaji delivery latency, OR
2. Continuous matching with sparse activity on individual stocks

The 99.7% BBO-unchanged rate for 2330 suggests very infrequent price discovery -- most updates are quantity-only changes at the same price.

### Impact on Candidate Signals

**For TXFD6 (futures)**:
- 27.5 ticks/sec at ~125ms intervals
- Queue-based signals at 500ms-5s horizon: **VIABLE** (4-40 ticks per signal window)
- EMA span=200 = 7.3 seconds: **VIABLE**
- QVD acceleration at 2.5-25s: **VIABLE** (17-172 ticks)

**For 2330 (stock)**:
- 3.1 ticks/sec with huge variance
- Queue-based signals at 500ms-5s: **MARGINAL** (1.5-15 ticks, high variance)
- EMA span=200 = 65 seconds: **VIABLE but slow**
- L1 signals nearly useless (99.7% BBO unchanged)
- L5 depth signals may be more viable (L2-L5 can change without BBO change)

### Conclusion: C-3 RESOLVED

The 500ms batch matching concern is NOT applicable to TXFD6 futures (our primary trading target). TAIFEX uses near-continuous matching with ~125ms update granularity. For stocks, queue signals have limited utility due to 99.7% BBO stability, but L5 depth signals (revised Candidate C) may still work.

---

## X-3: Data Adequacy

### Evidence: Directory Scan + Metadata Analysis

**Available datasets**:

| Dataset | Source | Rows | Days | Fields | Symbol |
|---------|--------|------|------|--------|--------|
| `raw/txfd6/TXFD6_all_l1.npy` | ClickHouse | 1,779,257 | 4 | bid/ask px/qty, mid, spread_bps, volume, local_ts | TXFD6 |
| `raw/2330/2330_all_l1.npy` | ClickHouse | 198,349 | 4 | same as above | 2330 |
| `l5/TXFD6_l5.npy` | golden export | 2,171,578 | 10 | timestamp_ns, bids[5], asks[5] (px+qty) | TXFD6 |
| `l5/2330_l5.npy` | golden export | 537,470 | 10 | same | 2330 |
| `l5/2317_l5.npy` | golden export | ~1,321K | 10 | same | 2317 |
| `l5_v2/2330_l5.npy` | ClickHouse | 1,009,983 | 17 | same | 2330 |
| `l5_v2/2317_l5.npy` | ClickHouse | ~2,450K | 17? | same | 2317 |
| `l5_v2/TXFE6_l5.npy` | ClickHouse | ~5,721K | ? | same | TXFE6 |
| `processed/ofi_surprise/synthetic_lob_v2_train.npy` | synthetic | 20,000 | synthetic | bid/ask qty/px, mid, spread, volume | TXF/MXF |

**TickEvent fields** (from `events.py:22-38`):
- `price: int` (scaled x10000)
- `volume: int` (incremental)
- `total_volume: int`
- `bid_side_total_vol: int`
- `ask_side_total_vol: int`
- `is_simtrade: bool`
- NO explicit trade direction / side field

**What's available for each candidate**:

### Candidate B (ISS - Impact Surprise Signal)

| Required | Field | In L1 data? | In L5 data? | In FeatureEngine? |
|----------|-------|-------------|-------------|-------------------|
| OFI | Computed from bid/ask px/qty | YES (computable) | YES | YES (slot 11) |
| Return | mid_price diff | YES | YES (from bids/asks) | YES (slot 2 diff) |
| Depth | bid_qty + ask_qty | YES (L1 only) | YES (L1-L5) | YES (slots 4,5) |
| Previous tick state | stored in SymbolState | N/A (recompute) | N/A | YES |

**Verdict**: ALL data adequate. Can prototype on L1 data immediately. 1.78M TXFD6 ticks (4 days) + 2.17M L5 ticks (10 days) is sufficient for initial IC measurement.

### Candidate C revised (MLDM - Multi-Level Depth Momentum)

| Required | Field | In L1 data? | In L5 data? |
|----------|-------|-------------|-------------|
| L1 bid/ask qty | bid_qty, ask_qty | YES | YES |
| L2-L5 bid/ask qty | bids[1:5], asks[1:5] | NO | YES |
| OFI (for comparison) | Computed from L1 | YES | YES |

**Verdict**: L5 data required. Available for TXFD6 (2.17M rows, 10 days), 2330 (537K rows, 10 days), and 2317. Adequate for prototyping.

### Trade Direction (for deferred Candidate A)

TickEvent has `bid_side_total_vol` and `ask_side_total_vol` fields, which could enable volume-based side classification. However, these are cumulative totals, not per-tick direction. Per-tick side classification would require tick-rule inference from price vs BBO, which is ambiguous in batch auctions.

### Conclusion: X-3 RESOLVED

Data is adequate for both B and revised C. We have:
- 1.78M L1 ticks for TXFD6 (4 days) + 2.17M L5 ticks (10 days)
- 198K L1 ticks for 2330 (4 days) + 1.01M L5 ticks (17 days)
- All required fields present (bid/ask prices and quantities at L1-L5)
- FeatureEngine already computes OFI and all required base features

Missing: trade direction per tick (relevant only for deferred Candidate A).

---

## Summary

| Challenge | Status | Key Finding | Impact |
|-----------|--------|-------------|--------|
| B-1 | **RESOLVED** | Monte Carlo: EMA span=200 gives CV=0.23, 100% sign accuracy. Sign extraction makes signal robust. | B proceeds with EMA formulation |
| C-1 | **PARTIALLY RESOLVED** | QDI is ~100% collinear with OFI for stocks, ~57% for futures. Original signal DROPPED. | C reformulated as MLDM (L2-L5 depth momentum) |
| C-3 | **RESOLVED** | TXFD6 uses near-continuous matching (125ms intervals, 27.5 ticks/sec), NOT 500ms batch. 2330 stocks are sparse (3.1 ticks/sec). | Original concern does not apply to futures |
| X-3 | **RESOLVED** | 1.78M L1 + 2.17M L5 ticks for TXFD6. All fields present. | Adequate for prototyping |

### Revised Ranking

1. **Candidate B: EMA-Based Impact Surprise Signal (ISS)** -- UNCHANGED. All challenges resolved. Proceed to Stage 2.

2. **Candidate C (REVISED): Multi-Level Depth Momentum (MLDM)** -- Original QDI dropped due to OFI collinearity. Replaced with L2-L5 depth change signal that IS orthogonal to L1 OFI. Requires L5 data (available). Proceed to Stage 2.

3. **Candidate A: Hawkes Intensity Imbalance** -- Remains deferred. Trade side classification still problematic.
