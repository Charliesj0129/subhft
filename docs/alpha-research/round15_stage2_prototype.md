# Round 15 — Stage 2 Prototype Report: LOB Price-Keyed KE

**Date**: 2026-03-25
**Alpha**: `lob_kinetic_energy_price_keyed` (Candidate C from Stage 1)
**Status**: Stage 2 Complete — **NEGATIVE IC, requires reformulation before Stage 3**

---

## 1. Implementation

### Formulation (Price-Keyed, per Stage 2 spec)

```
KE_bid = Σ_{i=1}^{5} bid_qty[i] × (mid - bid_price[i])²
KE_ask = Σ_{i=1}^{5} ask_qty[i] × (ask_price[i] - mid)²
LOB_momentum = (KE_bid - KE_ask) / (KE_bid + KE_ask + ε)   ∈ [-1, 1]
LOB_gravity_center = bid_gc - ask_gc
    where gc = Σ qty[i] × dist[i] / Σ qty[i]
```

Smoothing: 8-tick EMA. Clip: [-1, 1] for momentum.

### Files
- `research/alphas/lob_kinetic_energy/price_keyed.py` — LobPriceKeyedKE class
- `research/alphas/lob_kinetic_energy/validate_stage2.py` — Full validation pipeline
- `research/experiments/validations/lob_kinetic_energy/stage2_validation_results.json` — Raw results

### Data
- 4 days TXFD6 L5 snapshots from hftbacktest .npz files (2026-03-19 to 2026-03-24)
- 1,617,338 total L5 snapshots
- Reconstructed from bid/ask depth events (5 levels each side)

---

## 2. DC-3: Pooled Spearman IC

| Horizon | Momentum IC | Gravity IC | N valid |
|---------|-------------|------------|---------|
| h=10    | **-0.0049** | **-0.0245** | 1,617,298 |
| h=50    | -0.0053     | -0.0131     | 1,617,138 |
| h=200   | -0.0145     | -0.0123     | 1,616,538 |

### Interpretation

**Both signals have consistently NEGATIVE pooled IC across all horizons and all 4 days.**

- Momentum IC negative: when KE_bid > KE_ask (more depth-weighted energy on bid side), the price tends to go DOWN, not up. This is **counter to the naive hypothesis** that bid-side energy = buy support.
- Gravity IC more negative: the gravity center signal is stronger (IC -0.025 at h=10) but in the wrong direction for a directional alpha.
- The negative sign is consistent across all 4 days (not a single-day artifact).

### Why the sign is inverted

The **price-keyed** KE formulation measures *static depth distribution*. High KE_bid means large quantities placed far from mid on the bid side. In TXFD6 microstructure, this likely reflects:

1. **Informed order placement**: Large resting orders at deep bid levels signal *expected downward pressure* (they are placed to absorb an anticipated drop), not buy support.
2. **Market maker inventory management**: Passive MMs place more depth on the side they want to *reduce exposure to*, creating asymmetric depth that signals their directional view.
3. **Mechanical spread effect**: When spread widens, dist² increases, inflating KE. Wide spread = low liquidity = higher future volatility, which correlates with mean-reversion rather than continuation.

This finding is consistent with Pulido, Rosenbaum & Sfendourakis (2023, arXiv:2307.15599) who show that volume imbalance is an *optimal market-maker response* to anticipated price moves — the imbalance is predictive precisely because it encodes the MM's view.

**The gravity center signal (IC = -0.025) may be usable as a reversal/contrarian signal if the sign is flipped**, but this requires careful investigation to ensure it's not just proxying for depth_imbalance (see DC-2).

---

## 3. DC-2: Collinearity (Spearman Rank)

| Feature pair | Spearman r | Status |
|-------------|-----------|--------|
| momentum vs ofi_l1_raw | +0.002 | PASS |
| momentum vs depth_imbalance | **+0.703** | **FAIL** |
| momentum vs l1_imbalance | +0.128 | PASS |
| momentum vs cum_ofi | +0.033 | PASS |
| gravity vs ofi_l1_raw | -0.021 | PASS |
| gravity vs depth_imbalance | +0.090 | PASS |
| gravity vs l1_imbalance | -0.199 | PASS |

### Key Finding

**Momentum FAILS DC-2 against depth_imbalance (r=+0.703, threshold 0.7).**

This is because the price-keyed KE formulation `KE = Σ qty × dist²` is heavily dominated by the quantity term when price distances are small and similar (TXFD6 spread is typically 1-5 ticks, so dist varies little across L1-L5). The signal collapses toward a weighted sum of quantities, which is essentially depth_imbalance.

**Gravity center PASSES DC-2** against all features. The distance-weighted average is sufficiently different from simple quantity ratios.

---

## 4. DC-1: Per-Level Depth Delta IC (Spearman, ~1s forward return)

| Level | Mean IC | Std IC | Per-day ICs |
|-------|---------|--------|-------------|
| L1 | **+0.017** | 0.015 | [+0.043, +0.008, +0.004, +0.014] |
| L2 | +0.003 | 0.006 | [+0.014, +0.002, -0.000, -0.003] |
| L3 | +0.002 | 0.003 | [+0.004, -0.003, +0.004, +0.002] |
| L4 | -0.001 | 0.003 | [+0.003, -0.002, +0.001, -0.005] |
| L5 | +0.000 | 0.003 | [+0.005, -0.001, -0.002, -0.002] |

### Key Finding

**L1 dominates predictive content.** L1 depth delta has 5-8x higher IC than any deeper level. L3-L5 have IC indistinguishable from zero. This means:

1. "Active depth" on TXFD6 is essentially L1 only.
2. Aggregating L2-L5 depth adds noise, not signal.
3. The velocity-based KE formulation (impl.py, which tracks quantity *changes*) operating on L1 only is likely the better approach.

This finding is consistent with TXFD6 market structure: the L5 book on Taiwan futures is thin (typical qty 1-11 contracts per level), and informed flow concentrates at L1.

---

## 5. Execution Concerns

### Integer Overflow — ALL PASS

| Test | Result | Max KE value |
|------|--------|-------------|
| TXFD6 raw prices (~33000) | PASS | KE_bid=688, KE_ask=518 |
| TXFD6 x10000 scaled | PASS | KE_bid=6.88e10, KE_ask=5.18e10 |
| Extreme qty (1e6) | PASS | — |
| Zero depth | PASS | — |
| Single level | PASS | — |

Max observed values on real data are well within float64 range. Even with x10000 price scaling, KE values reach ~7e10, far from float64 limits (~1.8e308).

### Compute Cost

| Metric | Value |
|--------|-------|
| Microseconds per tick | **14.74 µs** |
| Verdict | OK (well under 1ms budget) |

The Python implementation is sufficiently fast for the 125ms TXFD6 tick interval. Rust promotion not needed.

---

## 6. Signal Statistics

| Signal | Mean | Std | P5 | P95 |
|--------|------|-----|-----|-----|
| momentum | -0.012 | 0.207 | -0.353 | +0.328 |
| gravity_center | +0.005 | 1.819 | -0.880 | +0.897 |

Both signals are well-behaved: near-zero mean, reasonable spread, no extreme outliers.

---

## 7. Conclusions and Recommendations

### What we learned

1. **Price-keyed KE momentum has negative IC** — the spatial distribution of depth predicts *reversal*, not continuation, on TXFD6. This is a meaningful microstructure finding.

2. **Momentum ≈ depth_imbalance** (r=0.70) — the formulation doesn't add enough beyond simple depth ratio. The dist² weighting doesn't differentiate enough when TXFD6 spreads are narrow.

3. **Gravity center is more promising** — passes DC-2 (low collinearity with all existing features), has stronger IC magnitude (-0.025 at h=10), and measures something genuinely different (distance-weighted depth asymmetry vs simple quantity ratio).

4. **L1 dominates on TXFD6** — deeper levels add noise. This validates deferring "active depth" and suggests that L1-focused features (like the velocity-based KE in impl.py) are more appropriate for this market.

5. **The velocity-based formulation (impl.py) has positive IC** — the earlier validation showed pooled IC = +0.007 at h=10 on L1 data. The quantity-change (kinetic) formulation captures different information than the static-structure (price-keyed) formulation.

### Recommended next steps

| Priority | Action | Rationale |
|----------|--------|-----------|
| P0 | **Investigate gravity center as reversal signal** | IC=-0.025, passes DC-2, genuinely novel. Flip sign and test as "depth gravity reversal" |
| P1 | **Keep velocity-based KE (impl.py)** | Positive IC on L1 data, orthogonal to price-keyed. Already passes all tests |
| P2 | **Drop price-keyed momentum** | Fails DC-2, negative IC, redundant with depth_imbalance |
| P3 | **Re-run gravity center validation with sign flip** | If IC flips to +0.025, this is a viable FeatureEngine candidate |

### Gate C readiness

- **Momentum**: NOT ready — fails DC-2, negative IC
- **Gravity center**: CONDITIONAL — needs sign investigation, potentially ready if IC holds as reversal signal
- **Velocity-based KE (impl.py)**: Partially ready — positive IC but low magnitude, needs L5 validation

---

## Appendix: Raw Results

Full results in `research/experiments/validations/lob_kinetic_energy/stage2_validation_results.json`.
