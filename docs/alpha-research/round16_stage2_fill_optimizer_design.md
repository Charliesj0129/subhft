# Round 16 Stage 2: FillOptimizer Prototype Design

**Date**: 2026-03-26
**Candidate**: C — Latency-Aware Queue-Optimal Order Placement
**Status**: Stage 2 (Prototype Design)
**Papers**: 2403.02572 (Lokin/Yu), 2502.18625 (Albers), 2508.20225 (Barzykin)

---

## 1. Architecture

```
Strategy.on_stats()
  → OrderIntent(side, price, qty)
    → RiskEngine.evaluate()
      → OrderCommand
        → [FillOptimizer.optimize(cmd, feature_tuple)]  ← NEW
          → OrderAdapter._dispatch_to_api()
```

FillOptimizer sits as a **composable middleware** in the OrderAdapter pipeline, following the ImbalanceTimer pattern. It intercepts OrderCommands after risk approval, evaluates fill probability, and can:
- **PASS**: forward the order unchanged (high fill probability)
- **ADJUST**: change placement distance (shift price by 1-2 ticks)
- **UPGRADE**: change from LMT to IOC (aggressive cross when fill prob is too low)
- **DEFER**: delay execution (like ImbalanceTimer, wait for better conditions)
- **SKIP**: drop the order (fill probability too low, conditions unfavorable)

## 2. Fill Probability Heuristic Model

### Available Features (lob_shared_v1, indices 0-15)

| Index | Feature | Use in Model |
|-------|---------|-------------|
| 3 | spread_scaled | Spread regime — wider = better fill quality |
| 6 | depth_imbalance_ppm | Book pressure direction |
| 8 | l1_bid_qty | Queue depth at best bid |
| 9 | l1_ask_qty | Queue depth at best ask |
| 10 | l1_imbalance_ppm | L1 queue ratio |
| 11 | ofi_l1_raw | Recent order flow direction |
| 13 | ofi_l1_ema8 | Smoothed order flow trend |
| 14 | spread_ema8_scaled | Spread stability |

### Heuristic Formula

Paper 2403.02572 (Lokin/Yu) derives fill probability as a function of queue depth and arrival rate. We approximate with observable features:

```
P_fill(side=BUY) = sigmoid(
    w0                                          # bias
  + w1 * normalize(ask_qty_l1 - bid_qty_l1)    # thin ask = easier buy fill
  + w2 * normalize(ofi_l1_ema8)                 # negative OFI = sell pressure = buy fills
  + w3 * normalize(spread_scaled)               # wider spread = more room
  + w4 * normalize(-bid_qty_l1)                 # less queue ahead = better position
)

P_fill(side=SELL) = sigmoid(
    w0
  + w1 * normalize(bid_qty_l1 - ask_qty_l1)    # thin bid = easier sell fill
  + w2 * normalize(-ofi_l1_ema8)               # positive OFI = buy pressure = sell fills
  + w3 * normalize(spread_scaled)
  + w4 * normalize(-ask_qty_l1)
)
```

Where:
- `normalize(x) = x / max(abs(x_running), 1)` using running max (no allocation, single scalar state)
- `sigmoid(x) = 1 / (1 + exp(-x))`
- Initial weights: `w0=-0.5, w1=0.3, w2=0.2, w3=0.3, w4=-0.2` (calibrate from backtest)

### Why These Features (Paper Grounding)

- **Queue depth** (w4): Lokin/Yu 2403.02572 — fill probability inversely proportional to queue size
- **Imbalance direction** (w1, w2): Albers 2502.18625 — contrarian placement (thick side) has lower fill prob but better post-fill returns; we track this tradeoff
- **Spread width** (w3): Barzykin 2508.20225 — wider spread reduces adverse selection cost, improves net edge even if fill probability is unchanged
- **OFI** (w2): momentum in order flow predicts short-term queue depletion direction

## 3. Decision Matrix

| Fill Prob | Spread Regime | OFI Alignment | Action | Price Adjustment |
|-----------|--------------|---------------|--------|-----------------|
| ≥ 0.6 | Any | Favorable | PASS | None (best price) |
| 0.4-0.6 | Wide (>2.5 bps) | Favorable | PASS | None |
| 0.4-0.6 | Wide (>2.5 bps) | Unfavorable | ADJUST | -1 tick (deeper) |
| 0.4-0.6 | Narrow (<2.5 bps) | Any | DEFER | Wait up to 500ms |
| 0.2-0.4 | Wide (>3.0 bps) | Favorable | ADJUST | -1 tick |
| 0.2-0.4 | Any other | Any | SKIP | Order dropped |
| < 0.2 | Any | Any | SKIP | Order dropped |

**Note**: UPGRADE to IOC not included in v1 — requires OrderIntent `price_type` extension (see Section 5). Deferred to v2.

## 4. FillOptimizer Class Interface

```python
class FillOptimizer:
    """
    Execution optimization layer for limit order fill quality.
    Follows ImbalanceTimer pattern: stateless per-call, composable middleware.

    Precision Law: All price params are scaled int. Fill probability is
    a float used only for threshold comparison, not price accounting.
    """

    __slots__ = (
        "_weights",
        "_spread_threshold_scaled",  # 2.5 bps in scaled int
        "_wide_spread_threshold_scaled",  # 3.0 bps in scaled int
        "_high_fill_threshold",
        "_medium_fill_threshold",
        "_low_fill_threshold",
        "_defer_timeout_ns",
        "_tick_size_scaled",
        "_running_max_qty",
        "_running_max_ofi",
        "_running_max_spread",
        "_enabled",
    )

    def __init__(
        self,
        *,
        weights: tuple[float, ...] = (-0.5, 0.3, 0.2, 0.3, -0.2),
        spread_threshold_bps: float = 2.5,
        wide_spread_threshold_bps: float = 3.0,
        high_fill_threshold: float = 0.6,
        medium_fill_threshold: float = 0.4,
        low_fill_threshold: float = 0.2,
        defer_timeout_ns: int = 500_000_000,  # 500ms
        tick_size_scaled: int = 10000,  # 1 point x10000
        enabled: bool = True,
    ) -> None: ...

    def estimate_fill_prob(
        self,
        side: int,  # Side.BUY=0, Side.SELL=1
        feature_tuple: tuple,
    ) -> float:
        """Estimate fill probability from LOB features. O(1), no allocation."""
        ...

    def optimize(
        self,
        side: int,
        price_scaled: int,
        feature_tuple: tuple,
    ) -> tuple[int, int, int]:
        """
        Returns (action, adjusted_price_scaled, defer_ns).

        action: 0=PASS, 1=ADJUST, 2=DEFER, 3=SKIP
        adjusted_price_scaled: new price (same as input if PASS/DEFER/SKIP)
        defer_ns: wait time in ns (0 if not DEFER)
        """
        ...

    def reset(self) -> None:
        """Reset running normalization state. Call at session start."""
        ...
```

## 5. OrderIntent Extension (v2, deferred)

For dynamic limit/market selection, add optional field:

```python
@dataclass(slots=True)
class OrderIntent:
    ...
    price_type: str = "LMT"  # "LMT" | "MKT" | "MKP"
```

Then in `OrderAdapter._dispatch_to_api()` line 575:
```python
# Before: price_type = self._broker_codec.encode_price_type(str(order_params.get("price_type", "LMT")))
# After:  price_type = self._broker_codec.encode_price_type(intent.price_type or str(order_params.get("price_type", "LMT")))
```

**Deferred to v2** — v1 uses price adjustment only (ADJUST action), not order type switching.

## 6. Integration Points

| Component | File | Line | Change |
|-----------|------|------|--------|
| FillOptimizer class | `src/hft_platform/execution/fill_optimizer.py` | NEW | ~150 LOC |
| OrderAdapter integration | `src/hft_platform/order/adapter.py` | ~494 (`_dispatch_to_api`) | Call `fill_optimizer.optimize()` before dispatch |
| Config | `config/base/main.yaml` | NEW section | `fill_optimizer:` with weights and thresholds |
| Feature access | `src/hft_platform/order/adapter.py` | constructor | Accept feature_tuple_source callback |
| Metrics | `src/hft_platform/observability/metrics.py` | append | 4 new counters + 1 histogram |
| Unit tests | `tests/unit/test_fill_optimizer.py` | NEW | ~200 LOC |
| Backtest adapter | `src/hft_platform/backtest/adapter.py` | TBD | Wire FillOptimizer into backtest path |

## 7. Metrics & Observability

```
hft_fill_optimizer_decision_total{action="pass|adjust|defer|skip"}  # Counter
hft_fill_optimizer_fill_prob                                        # Histogram
hft_fill_optimizer_adjustment_ticks                                 # Histogram
hft_fill_optimizer_defer_duration_ns                                # Histogram
```

## 8. Backtest Validation Plan

### A/B Comparison
- **Control**: OpportunisticMM without FillOptimizer (current baseline)
- **Treatment**: OpportunisticMM + FillOptimizer
- **Data split**: 10 days IS (calibrate weights), 4 days OOS (validate)
- **L1 fill assumption**: mid-cross (optimistic baseline)
- **L2 fill assumption**: queue-back (conservative)

### Key Metrics
- Net PnL per RT (bps) — primary
- Fill rate (fills / signals) — must not collapse to near-zero
- Adverse selection rate (markout 1s, 5s, 10s post-fill)
- Sharpe ratio (daily, annualized)
- Trade count per day — must be > 20 for statistical relevance

### Statistical Significance
- 14 days total = 14 independent samples (daily PnL)
- Paired t-test or Wilcoxon signed-rank for A/B comparison
- Minimum detectable effect: ~0.3 bps/RT improvement (power analysis needed)
- DSR (Deflated Sharpe Ratio) to correct for multiple testing

## 9. Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Overfitting weights on 14 days | False positive improvement | Walk-forward validation, L2 sensitivity |
| Fill prob model inaccurate | SKIP too many orders → zero PnL | Conservative thresholds, fallback to PASS |
| DEFER causes stale orders | Fills at worse prices | Strict 500ms timeout, cancel on timeout |
| Running max normalization drift | Model performance degrades intraday | Reset at session start, clip to bounds |
| Hot-path latency | Exceeds 250us budget | Pre-computed sigmoid table, no allocation |

## 10. Timeline

- **Day 1-3**: FillOptimizer class + unit tests (TDD)
- **Day 4-5**: Integration into OrderAdapter + wiring feature access
- **Day 6-8**: Backtest framework + IS calibration
- **Day 9-10**: OOS validation + A/B analysis
- **Day 11-12**: Parameter sensitivity + report
- **Day 13-14**: Buffer / Challenger/Execution review responses

Total: **2 weeks**
