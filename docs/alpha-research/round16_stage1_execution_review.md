# Execution Review -- Round 16 Stage 1 (Revised)

**Reviewer**: Execution Reviewer
**Date**: 2026-03-25
**Subject**: OpportunisticMM improvement candidates -- Latency-Aware Skew, Toxic Flow, Fill Probability
**Codebase refs**: `src/hft_platform/strategies/opportunistic_mm.py`, `src/hft_platform/feature/engine.py`, `config/research/latency_profiles.yaml`

---

## Platform Specifications (Verified)

| Parameter | Value | Source |
|-----------|-------|--------|
| Broker RTT P95 submit | 36 ms | `config/research/latency_profiles.yaml` (shioaji_sim_p95_v2026-03-04) |
| Broker RTT P95 modify | 43 ms | Same |
| Broker RTT P95 cancel | 47 ms | Same |
| Internal pipeline latency | 250 us | Same |
| FeatureEngine version | `lob_shared_v1` (16 features, schema_version=1) | `src/hft_platform/feature/registry.py` |
| StormGuard WARM | -50 bps | `src/hft_platform/risk/storm_guard.py` |
| StormGuard STORM | -100 bps | Same |
| StormGuard HALT | -200 bps | Same |
| Max position (SimpleMM) | 100 lots | `src/hft_platform/strategies/simple_mm.py` line 68 |
| Max position (risk validator) | 1000 lots | `src/hft_platform/risk/validators.py` |
| TXFD6 L1 data | 14 daily files (2026-01-26 to 2026-03-24) + merged | `research/data/raw/txfd6/` |
| TXFD6 L2 data | 4 daily files (2026-03-19 to 2026-03-24), 5 levels, ~4M rows/day | Same |
| TXFD6 median tick interval | 125 ms | Prior research (Round 13) |

### FeatureEngine v1 Feature Tuple (16 features, indices 0-15)

| Index | Feature ID | Type | Scale |
|-------|-----------|------|-------|
| 0 | best_bid | i64 | x10000 |
| 1 | best_ask | i64 | x10000 |
| 2 | mid_price_x2 | i64 | x10000 |
| 3 | spread_scaled | i64 | x10000 |
| 4 | bid_depth | i64 | - |
| 5 | ask_depth | i64 | - |
| 6 | depth_imbalance_ppm | i64 | x1000000 |
| 7 | microprice_x2 | i64 | x10000 |
| 8 | l1_bid_qty | i64 | - |
| 9 | l1_ask_qty | i64 | - |
| 10 | l1_imbalance_ppm | i64 | x1000000 |
| 11 | ofi_l1_raw | i64 | - |
| 12 | ofi_l1_cum | i64 | - |
| 13 | ofi_l1_ema8 | i64 | - |
| 14 | spread_ema8_scaled | i64 | x10000 |
| 15 | depth_imbalance_ema8_ppm | i64 | x1000000 |

**Note**: `lob_shared_v2` (18 features with `mlofi_gradient_x1000`, `impact_surprise_x1000`, `deep_depth_momentum_x1000`) is referenced in project memory but **does not exist in the codebase**. Only v1 is registered in `default_feature_registry()`.

---

## Candidate A: Latency-Aware Inventory Skew Optimization -- APPROVE (conditional)

**Papers**: Barzykin 2603.07752, Relaver 2505.12465, Albers 2502.18625

### Latency Compatibility: PASS

- Barzykin 2603.07752 derives closed-form optimal quotes parameterized by latency. The framework accepts arbitrary latency inputs; it does not assume sub-millisecond execution.
- Our Shioaji P95 profile (submit=36ms, modify=43ms, cancel=47ms) can be plugged directly into the Barzykin framework's latency parameter.
- Relaver 2505.12465 and Albers 2502.18625 similarly model latency as a first-class parameter rather than assuming zero-latency.
- **Config drift = 0**: Paper parameters map directly to our measured latency profile.

### Feature Availability: PASS

Required features and their availability:
- `mid_price_x2` [2] -- available
- `spread_scaled` [3] -- available
- `depth_imbalance_ppm` [6] or `l1_imbalance_ppm` [10] -- available
- `microprice_x2` [7] -- available
- `ofi_l1_ema8` [13] -- available
- `spread_ema8_scaled` [14] -- available (usable as volatility proxy)
- Inventory position -- available via `self.position(symbol)` in BaseStrategy

No new FeatureEngine features required. Volatility can be derived from `spread_ema8_scaled` as a proxy or computed from tick data in `TickEvent` (realized vol from price series).

### Integration Path: CLEAR

- Current `SimpleMarketMaker` inventory skew is trivial: `skew_x2 = -(pos * tick_size * 2) / INVENTORY_SKEW_DIVISOR` with hardcoded divisor of 5 (`simple_mm.py` lines 47-53).
- Barzykin's optimal skew replaces this with: `skew = gamma * inventory * sigma^2 * tau`, where `gamma` = risk aversion, `sigma` = volatility, `tau` = remaining time/latency horizon.
- Integration point: Override `on_stats()` in `OpportunisticMM` (or create subclass `LatencyAwareMM`) to replace the linear skew with the Barzykin formula. `OpportunisticMM` already overrides `on_stats()` and delegates to `super().on_stats()` for quoting -- the skew computation is in `SimpleMarketMaker.on_stats()`.
- The `ImbalanceTimer` (`src/hft_platform/execution/imbalance_timer.py`) already exists and can compose with latency-aware skew for entry timing.
- Precision Law compliance: gamma/sigma parameters are pre-computed floats used only for skew magnitude; all price calculations remain scaled integers.

### Implementation Estimate: 2-3 weeks REALISTIC

- Week 1: Barzykin closed-form skew in new `LatencyAwareMM` strategy class. Unit tests with known inputs. ~300 LOC.
- Week 2: Backtest on TXFD6 L1 data (14 days). Tick-based realized vol estimator (~100 LOC).
- Week 3: Parameter sensitivity analysis (gamma, vol window). Integration with `OpportunisticMM` spread gate.
- Risk: Volatility estimation quality on 125ms median tick intervals. Mitigated by using EMA-smoothed spread as proxy.

### Risk Limits Alignment: PASS

- Expected 0.5-1.0 bps adverse selection reduction = smaller drawdowns per trade.
- No increase in max position (capped at `max_pos=100` in SimpleMM, `max_position_lots=1000` in risk validators).
- Strategy reduces risk exposure by better skewing, not by increasing position size.
- Worst-case drawdown unchanged; skew optimization cannot increase loss beyond existing position limits.

### Data Availability: PASS

- 14 days L1 + 4 days L2 (5-level) data in `research/data/raw/txfd6/`.
- L1: ~408k rows/day with `bid_px`, `ask_px`, `bid_qty`, `ask_qty`, `mid_price`, `spread_bps`, `volume`, `local_ts`.
- Sufficient for backtesting skew optimization. L2 available for deeper queue analysis.

---

## Candidate B: Toxic Flow Classification -- REJECT

**Papers**: Cartea & Sanchez-Betancourt 2503.18005

### Latency Compatibility: CONDITIONAL PASS

- The 4-coefficient linear model is computationally trivial and compatible with 36ms latency.
- However, the paper assumes real-time trade classification (aggressor side identification) which creates a critical data dependency.

### Feature Availability: FAIL -- CRITICAL GAP

- **Trade-side classification is missing from `TickEvent`**. The `TickEvent` struct (`src/hft_platform/events.py` lines 22-37) has `bid_side_total_vol` and `ask_side_total_vol` but NO per-tick aggressor side flag.
- The only aggressor inference in the codebase exists in `strategies/rust_alpha.py` (lines 213-223) as a heuristic (`is_aggressor_buy` based on price vs mid). This is not a proper Lee-Ready or tick-rule classifier.
- The Cartea model requires a continuous toxicity score derived from classified trade flow. Without reliable trade-side classification at the tick level, the toxicity metric is uncomputable.
- This gap has been identified three times previously:
  1. Hawkes Intensity deferred -- "no trade-side classification in TickEvent" (alpha microstructure research)
  2. ISS feature research -- same blocker
  3. Now Candidate B -- same blocker
- **Config drift > 0**: Paper assumes trade-side-classified flow data. We have aggregate volume only. Building a Lee-Ready classifier adds 2-3 weeks before the toxic flow model can begin.

### Integration Path: BLOCKED

- Even with trade classification, the 4-coefficient optimization requires a training pipeline to fit coefficients on historical data with labeled toxic/non-toxic periods.
- No training infrastructure exists in the strategy framework. Strategies are stateless rule-based systems.
- Would require: (1) trade classifier module in TickEvent pipeline, (2) toxicity scoring service, (3) coefficient training pipeline, (4) real-time toxicity feature in FeatureEngine.

### Implementation Estimate: 3-4 weeks UNREALISTIC

Actual estimate with missing infrastructure: **6-8 weeks minimum**:
- 2-3 weeks: Lee-Ready trade classifier + TickEvent pipeline integration
- 1 week: Toxicity scoring feature in FeatureEngine
- 2 weeks: 4-coefficient optimization + backtesting
- 1 week: Integration with OpportunisticMM

The researcher's 3-4 week estimate assumes trade classification exists. It does not.

### Risk Limits Alignment: INCONCLUSIVE

Cannot assess without implementable toxicity scores.

### Data Availability: PARTIAL FAIL

- L1 data has aggregate `bid_side_total_vol` / `ask_side_total_vol` but no per-trade aggressor labels.
- L2 hftbt data has `ev`, `px`, `qty` fields but no explicit trade direction.
- Trade classification would need to be inferred heuristically, adding noise to the toxicity metric.

---

## Candidate C: Fill Probability-Conditioned Entry Filter -- APPROVE

**Papers**: Lokin & Yu 2403.02572, Albers 2502.18625

### Latency Compatibility: PASS

- Lokin & Yu 2403.02572 models fill probability as a function of queue position, spread, and time-in-force. The model explicitly accounts for execution latency in the fill probability estimate.
- Albers 2502.18625 maps our 36ms submit latency to an expected queue-position disadvantage, which is the direct input to the fill probability function.
- **Config drift = 0**: Fill probability is parameterized by latency; our measured 36ms fits directly.

### Feature Availability: PASS

Required features and their availability:
- `spread_scaled` [3] -- available
- `l1_bid_qty` [8] -- available (queue depth for fill probability)
- `l1_ask_qty` [9] -- available
- `depth_imbalance_ppm` [6] -- available
- `microprice_x2` [7] -- available
- `ofi_l1_raw` [11] -- available (for recent fill rate proxy)

No new FeatureEngine features required. Queue-position estimation is a local computation within the strategy (~50 LOC).

### Integration Path: CLEAR

- `OpportunisticMM` has a binary spread gate: `spread_bps < threshold -> cancel` (line 60). The fill probability filter adds a second gate: `if fill_prob(queue_pos, spread, latency) < threshold -> skip entry`.
- Integration point: Between the spread gate (line 60) and `super().on_stats(event)` delegation (line 71) in `OpportunisticMM.on_stats()`.
- Composable with `ImbalanceTimer` (`src/hft_platform/execution/imbalance_timer.py`): enter only when BOTH imbalance is favorable AND fill probability exceeds threshold. The `ImbalanceTimer` pattern already demonstrates this conditional gating architecture.
- Precision Law safe: fill probability is a [0,1] float used only for threshold comparison, not price accounting.

### Implementation Estimate: 2 weeks REALISTIC

- Week 1: Fill probability estimator based on Lokin & Yu queue model. Unit tests with synthetic LOB states. ~200-300 LOC.
- Week 2: Backtest on TXFD6 L1+L2 data. Integrate into `OpportunisticMM`. Parameter sweep for fill probability threshold.
- Simplest of the three candidates. No new infrastructure needed.

### Risk Limits Alignment: PASS

- Expected 0.3-0.7 bps saving per filtered trade. The filter REDUCES trade frequency (fewer but higher-quality entries), naturally reducing drawdown.
- No increase in position limits or risk exposure. StormGuard thresholds unaffected.
- Conservative max drawdown: within current WARM threshold (-50 bps).

### Data Availability: PASS

- L1 data (14 days): sufficient for fill probability calibration (`bid_qty`, `ask_qty`, `spread_bps`).
- L2 data (4 days, 5 levels, ~4M rows/day): queue depth at multiple levels for accurate fill probability estimation.
- 14 L1 + 4 L2 days adequate for IS/OOS split (10 IS / 4 OOS).

---

## Overall Verdict

| Candidate | Verdict | Config Drift | Key Blocker |
|-----------|---------|-------------|-------------|
| **A: Latency-Aware Inventory Skew** | **APPROVE** (conditional) | 0 | Vol estimation quality; mitigated by spread-EMA proxy |
| **B: Toxic Flow Classification** | **REJECT** | >0 | No trade-side classification in TickEvent; true estimate 6-8 weeks |
| **C: Fill Probability Entry Filter** | **APPROVE** | 0 | None |

### Recommended Execution Order

1. **Candidate C first** (2 weeks): Lowest risk, clearest integration path, immediate value. Provides measurable baseline improvement to OpportunisticMM before tackling the more complex skew optimization.

2. **Candidate A second** (2-3 weeks, after C): Builds on improved OpportunisticMM. The Barzykin skew and fill probability filter are orthogonal improvements that compose well -- skew optimizes WHERE to quote, fill filter optimizes WHEN to quote.

3. **Candidate B deferred to Round 17+**: Requires foundational infrastructure (trade-side classifier in TickEvent, toxicity scoring in FeatureEngine). This infrastructure benefits the broader platform beyond just this candidate.

### Infrastructure Gap Flagged

**Trade-side classification**: This is the THIRD time missing trade-side classification has blocked a research candidate (Hawkes Intensity, ISS feature, now Candidate B). Recommend prioritizing a Lee-Ready or tick-rule classifier in `TickEvent` as a P2 platform infrastructure item. Estimated effort: 1-2 weeks standalone. Once available, Candidate B and other flow-toxicity research become immediately unblocked.
