# R18 Stage 1 Execution Review — TMFD6 Conditional Market Making

**Date**: 2026-03-26
**Reviewer**: Execution Agent
**Artifact reviewed**: `docs/alpha-research/round18_stage1_survey.md`
**Latency profile**: `shioaji_sim_p95_v2026-03-04`

---

## VERDICT: CONDITIONAL APPROVE

Conditions:
1. Direction B (SG-LP) proceeds to Stage 2 as primary — simplest, fewest infrastructure gaps
2. Direction A (RCM) proceeds to Stage 2 as secondary — requires empirical reversal rate measurement on TMFD6 before committing to classifier build
3. Direction C (IBH) is DEFERRED — only proceed if both A and B show independent viability in Stage 2
4. Stage 2 must begin with a data-only measurement phase (adverse selection rate, fill rate at wide spreads) before any strategy implementation

---

## 1. Latency Feasibility

### Profile
| Operation | P95 RTT (ms) | Live uplift (1.5x) |
|-----------|-------------|---------------------|
| Place     | 36          | 54                  |
| Modify    | 43          | 64.5                |
| Cancel    | 47          | 70.5                |
| Internal pipeline | 0.055 ms | — |

### Direction A (RCM) — FEASIBLE with caveats

- **Signal half-life**: RCM reacts to OBI "incorrectness" — a state that persists for multiple ticks (seconds, not milliseconds). At TMFD6's 1.8 ticks/sec cadence, a reversal condition lasts ~500ms-2s. This is well within 36ms place latency.
- **Queue position concern**: The strategy posts limit orders at the touch. At 36ms RTT, the order arrives ~36ms after the signal. With TMFD6's 4.1-lot average L1 depth and 1.8 ticks/sec, this means joining a ~4-lot queue. Queue position is NOT competitive vs co-located HFTs, but TMFD6's thin retail-dominated book may have fewer HFT participants.
- **Critical**: Albers' results assume competitive queue position on Binance (sub-ms latency). At 36ms, our queue position will be consistently at the back. This degrades fill quality (we get filled precisely when adverse selection is worst — the back-of-queue adverse fill problem documented by DeLise 2024).
- **Verdict**: Latency is technically sufficient for posting orders, but queue position degradation is a structural disadvantage. Signal half-life >> RTT, so the issue is not signal staleness but fill quality.

### Direction B (SG-LP) — FEASIBLE

- **Signal half-life**: The spread gate (>= 5 pts) is a slow-changing regime — median duration of wide-spread episodes on TMFD6 is likely in the 10s-60s range (must be measured in Stage 2). This is orders of magnitude longer than broker RTT.
- **Quote update cycle**: At 43ms modify RTT, the strategy can update quotes ~23 times/sec. With TMFD6's 1.8 ticks/sec, this gives ~13 quote updates per tick — more than sufficient.
- **Stale quote risk**: During fast moves (e.g., 3 ticks in 500ms), quotes are stale for 43ms during modification. At TMFD6's typical 4-pt spread, a 1-tick (1-pt) adverse move during 43ms stale window is a ~0.33 bps cost — manageable.
- **Verdict**: Fully compatible with latency profile. The spread-gate regime changes slowly relative to RTT.

### Direction C (IBH) — FEASIBLE but latency-stressed

- **A-S quote updates**: The Avellaneda-Stoikov framework assumes continuous quote adjustment. At 43ms modify RTT, the strategy operates in discrete 43ms steps — effectively a discrete-time approximation.
- **Inventory unwind**: Hard cap at 1-2 lots means aggressive unwind (crossing the spread) when at cap. At 36ms place + 36ms fill notification, the unwind cycle is ~72ms minimum. During volatile periods, the position could move further adverse in this window.
- **Quote staleness during fast moves**: With 4+ parameters driving quote placement (gamma, inventory, microprice, spread), any parameter change requires a full modify cycle (43ms). Multiple simultaneous changes create compounding staleness.
- **Verdict**: Latency is technically feasible but creates meaningful approximation error in the A-S framework. The discrete-time effect needs explicit modeling in backtest (not just assume continuous quotes).

---

## 2. Feature Availability

### Available in FeatureEngine v2 (`lob_shared_v2`, 21 features)

| Feature | Index | Needed by | Status |
|---------|-------|-----------|--------|
| `best_bid` | 0 | A, B, C | Available |
| `best_ask` | 1 | A, B, C | Available |
| `mid_price_x2` | 2 | A, B, C | Available |
| `spread_scaled` | 3 | A, B, C | Available |
| `bid_depth` / `ask_depth` | 4, 5 | A, B, C | Available |
| `depth_imbalance_ppm` | 6 | A, C | Available |
| `microprice_x2` | 7 | A, B, C | Available |
| `l1_bid_qty` / `l1_ask_qty` | 8, 9 | A, B, C | Available |
| `l1_imbalance_ppm` | 10 | A (OBI signal) | Available |
| `ofi_l1_raw` / `ofi_l1_cum` / `ofi_l1_ema8` | 11, 12, 13 | A, C | Available |
| `spread_ema8_scaled` | 14 | B (spread regime) | Available |
| `depth_imbalance_ema8_ppm` | 15 | A, C | Available |
| `ofi_depth_norm_ppm` | 16 | A (reversal signal) | Available |
| `ret_autocov_5s_x1e6` | 17 | A (reversal signal) | Available |
| `tob_survival_ms` | 18 | A (reversal signal) | Available |
| `impact_surprise_x1000` | 19 | — | Available (provisional) |
| `deep_depth_momentum_x1000` | 20 | — | Available (provisional) |

### NOT available — must be built

| Feature | Needed by | Complexity | Notes |
|---------|-----------|------------|-------|
| `phi_8min` (8-min momentum) | C | MEDIUM | R17 identified IC=0.041 but never implemented in FeatureEngine. Requires 8-min rolling window over mid-price returns. ~80 LOC new feature kernel. |
| Reversal classifier (logistic) | A | HIGH | Albers' 4-feature-group model. Must be trained on TMFD6 data. Not a FeatureEngine feature — it's a strategy-level model. |
| Order arrival rate (lambda) | B, C | MEDIUM | Not a real-time feature — offline calibration from ClickHouse data. Needed for A-S parameter estimation. |
| Queue position estimator | A, B, C | HIGH | No queue position tracking infrastructure exists. Would need to track our order's position in the queue based on depth changes. ~200-300 LOC new component. |

### Assessment

Directions B and A have good feature coverage from the existing FeatureEngine v2. The `OpportunisticMM` strategy (already in codebase) implements a spread-gate + reversal filter pattern on TXFD6 — highly reusable for Direction B on TMFD6. The main gap is `phi_8min` for Direction C and the reversal classifier for Direction A.

---

## 3. Data Sufficiency

### Available: 21 days L1 (9.16M rows, Jan 26 - Mar 26, TMFD6)

| Candidate | Minimum for significance | 21 days sufficient? | Notes |
|-----------|-------------------------|---------------------|-------|
| B (SG-LP) | ~10 days (spread regime is high-frequency: 45.5% of time at >= 5 pts) | YES | Spread distribution stable across days. 10 days gives ~1000+ wide-spread episodes for adverse selection measurement. |
| A (RCM) | ~15-20 days (reversal events are rarer: ~15% of OBI predictions) | MARGINAL | At 1.8 ticks/sec x 300 min x 21 days = ~680k ticks. ~15% reversals = ~100k events. Sufficient for logistic regression training but tight for OOS validation (need train/test split). |
| C (IBH) | ~30+ days (multi-parameter system, combinatorial regime space) | NO | 21 days is insufficient for robust calibration of 4+ parameters across regimes. Would require expanding data window or using TXFD6 as auxiliary calibration source. |

### Key data gaps

1. **No trade-side classification**: Albers' reversal model uses "recent trades" as a feature group. TMFD6 TickEvents contain trade prices but not buyer/seller-initiated flag. This limits the reversal model's feature space.
2. **21 days spans Jan-Mar**: This includes Chinese New Year holiday effects, potential contract rolls, and seasonal patterns. Not a stable sample window for calibration.
3. **L1 only**: All three directions claim L1 is sufficient, which is correct for base implementation. L2 data (depth levels 2-5) would improve microprice accuracy but is not a blocker.

---

## 4. Infrastructure Gaps

### Existing reusable components

| Component | File | Reusable for |
|-----------|------|-------------|
| `SimpleMarketMaker` | `strategies/simple_mm.py` | B, C — symmetric quoting, inventory skew |
| `OpportunisticMM` | `strategies/opportunistic_mm.py` | B — spread gate + reversal filter already implemented for TXFD6 |
| `CascadeBounceStrategy` | `strategies/cascade_bounce.py` | — session gating, stop-loss, cooldown patterns |
| `ImbalanceTimer` | `execution/imbalance_timer.py` | B, C — delay entry until favorable imbalance |
| `BaseStrategy` | `strategy/base.py` | All — `buy()/sell()/cancel()` with TIF.LIMIT support |
| FeatureEngine v2 | `feature/engine.py` + `feature/registry.py` | All — 21 features available via `FeatureUpdateEvent` |
| Risk engine + StormGuard | `risk/engine.py`, `risk/storm_guard.py` | All — HALT blocks new orders, cancel allowed |

### New components needed

| Component | Needed by | Effort (LOC) | Priority |
|-----------|-----------|-------------|----------|
| **Spread-regime gate** (adapt OpportunisticMM thresholds to TMFD6 4-pt cost) | B | ~50 | P0 — trivial adaptation |
| **Adverse selection measurement script** (offline, ClickHouse query) | B, A | ~150 | P0 — must run before any strategy build |
| **Fill rate measurement script** (offline, ClickHouse query) | B, A | ~100 | P0 — must run before any strategy build |
| **phi_8min feature kernel** (8-min rolling momentum) | C | ~80 | P1 — only if C proceeds |
| **Reversal classifier** (logistic regression, 4 feature groups) | A | ~200-300 | P1 — training + inference code |
| **Limit order lifecycle manager** (track open orders, detect fills, manage cancel/replace) | A, B, C | ~200-400 | P1 — critical for any maker strategy. `BaseStrategy.place_order()` exists but no lifecycle tracking. |
| **Queue position estimator** | A | ~200-300 | P2 — enhancement, not blocker |
| **A-S parameter calibration** (gamma, k, A from TMFD6 data) | C | ~150-200 | P2 — only if C proceeds |

### Critical gap: Limit order lifecycle management

The existing `BaseStrategy` has `buy()/sell()/cancel()` methods with `TIF.LIMIT` support, but there is **no infrastructure for tracking open limit orders, detecting partial fills, or managing cancel-replace workflows**. All existing strategies (SimpleMarketMaker, OpportunisticMM) operate in a fire-and-forget mode — they generate `OrderIntent` and rely on the execution pipeline.

For a **maker** strategy, the following lifecycle management is essential:
- Track which limit orders are currently open (per side, per price level)
- Detect when an order is filled (fully or partially) via `FillEvent` callback
- Cancel stale quotes when conditions change (spread tightens, inventory cap hit)
- Amend orders (price/qty) without cancel-replace race conditions

This is the **single largest infrastructure gap** for R18. Estimated effort: 200-400 LOC for a `LimitOrderManager` component, plus integration with the existing execution pipeline.

---

## 5. Risk Compatibility

### Existing risk engine capabilities

| Capability | Status | R18 compatibility |
|-----------|--------|-------------------|
| StormGuard FSM (NORMAL -> STORM -> HALT) | Active | Compatible — HALT blocks new quotes, allows cancels |
| Position limits per symbol | Active | Compatible — 1-2 lot cap maps to existing config |
| Circuit breaker (loss threshold) | Active | Compatible — daily loss limit |
| Idempotency / dedup | Active | Compatible — prevents duplicate order submission |
| Exposure tracking | Active | Compatible — single symbol (TMFD6) well within limits |

### Risk gaps for maker strategies

1. **Two-sided exposure**: Existing risk checks evaluate individual `OrderIntent`s. A maker strategy has both bid and ask orders outstanding simultaneously. The risk engine must understand that the **net** exposure is bounded by the inventory cap, not the gross open order notional.
2. **Quote cancellation on HALT**: When StormGuard triggers HALT, all outstanding limit orders must be cancelled immediately. The strategy must have a `cancel_all()` hook or the risk engine must forcibly cancel open quotes. This integration does not exist.
3. **Inventory cap enforcement**: The 1-2 lot hard cap must be enforced at the risk layer, not just the strategy layer. If both sides fill simultaneously (race condition), the position could momentarily exceed the cap. The risk engine should reject orders that would exceed the cap post-fill.

### Assessment

Existing risk infrastructure is broadly compatible. The two-sided exposure accounting and HALT-triggered cancel-all are the main gaps. Both are medium-effort enhancements (~100-150 LOC total).

---

## 6. Implementation Complexity Estimates

### Direction B (SG-LP) — LOWEST complexity

| Phase | Work | LOC | Dependencies |
|-------|------|-----|-------------|
| Data measurement | Adverse selection + fill rate scripts | ~250 | ClickHouse data |
| Strategy | Adapt `OpportunisticMM` -> `SpreadGatedLP` for TMFD6 cost model | ~200 | Existing OpportunisticMM |
| Limit order mgmt | Basic `LimitOrderManager` (open/filled/cancelled tracking) | ~250 | BaseStrategy extensions |
| Risk integration | Two-sided exposure + cancel-on-HALT | ~100 | Risk engine |
| Config + tests | TMFD6 params, unit tests | ~200 | — |
| **Total** | | **~1000** | **2-3 days** |

### Direction A (RCM) — MEDIUM complexity

| Phase | Work | LOC | Dependencies |
|-------|------|-----|-------------|
| Data measurement | Reversal frequency on TMFD6, OBI prediction accuracy | ~200 | ClickHouse data |
| Reversal classifier | Logistic regression (train + inference) | ~300 | scikit-learn (offline) |
| Strategy | `ReversalConditionalMaker` class | ~300 | LimitOrderManager from B |
| Feature gap | Trade-side classification (if feasible) | ~100-200 | TickEvent data |
| Config + tests | | ~200 | — |
| **Total** | | **~1100-1200** | **3-4 days** (after B's LimitOrderManager) |

### Direction C (IBH) — HIGHEST complexity

| Phase | Work | LOC | Dependencies |
|-------|------|-----|-------------|
| phi_8min feature | New FeatureEngine kernel | ~80 | FeatureEngine |
| A-S calibration | Parameter estimation from TMFD6 data | ~200 | ClickHouse data |
| Strategy | `InventoryBoundedHybrid` class (A-S + spread gate + reversal + phi) | ~400-500 | LimitOrderManager + B + A |
| Inventory manager | Aggressive unwind logic, cap enforcement | ~150 | Risk engine |
| Config + tests | | ~250 | — |
| **Total** | | **~1100-1200** | **4-5 days** (after A and B) |

---

## Blockers for Stage 2

### MUST resolve before any strategy implementation

1. **[BLOCKER-E1] Adverse selection rate measurement**: Run offline analysis on TMFD6 ClickHouse data to measure adverse selection rate conditional on spread width. If adverse_rate > 70% at spread >= 5 pts, **kill all three directions** (the fundamental premise is false). Estimated: 2-4 hours of data analysis.

2. **[BLOCKER-E2] Fill rate at wide spreads**: Measure fill probability for hypothetical limit orders at the touch during wide-spread periods. If fill_rate < 30%, the strategy has insufficient throughput. Estimated: 2-4 hours.

3. **[BLOCKER-E3] Limit order lifecycle manager**: Design and implement `LimitOrderManager` before any maker strategy can run in simulation. This is shared infrastructure for all three directions.

### Should resolve before Direction A

4. **[BLOCKER-E4] Reversal frequency on TMFD6**: Measure how often OBI incorrectly predicts next move on TMFD6. If reversal_rate < 10%, Direction A has insufficient signal. This is a pure data analysis task.

5. **[BLOCKER-E5] Trade-side classification gap**: TickEvent has price + volume but no buyer/seller-initiated flag. Albers' reversal model needs recent trade direction. Must determine if Lee-Ready or tick-rule classification is feasible from L1 data, or if this feature group must be dropped.

---

## Summary Table

| Direction | Latency | Features | Data | Infra gaps | Risk | Effort | Recommend |
|-----------|---------|----------|------|-----------|------|--------|-----------|
| **B (SG-LP)** | PASS | 90% available | 21d sufficient | LimitOrderManager (shared) | Minor gaps | ~1000 LOC / 2-3d | PRIMARY |
| **A (RCM)** | PASS (queue concern) | 80% available | Marginal (21d tight for train+test) | Reversal classifier + B's infra | Minor gaps | ~1200 LOC / 3-4d | SECONDARY |
| **C (IBH)** | PASS (discrete approx) | 70% available | Insufficient (need 30d+) | phi_8min + A-S calibration + A+B infra | Two-sided exposure | ~1200 LOC / 4-5d | DEFERRED |

---

## Recommended Stage 2 Execution Plan

**Week 1 (data phase)**:
1. Run BLOCKER-E1 and E2 (adverse selection + fill rate measurement). Kill gate: if either fails, stop all R18 work.
2. Run BLOCKER-E4 (reversal frequency). Determines if Direction A is viable.

**Week 1-2 (infrastructure)**:
3. Build `LimitOrderManager` (BLOCKER-E3). Shared by all directions.
4. Adapt `OpportunisticMM` to TMFD6 cost model for Direction B prototype.

**Week 2+ (strategy)**:
5. Direction B prototype + backtest on 21-day data.
6. If B shows promise AND reversal frequency > 10%, proceed with Direction A.
7. Direction C only if both A and B are independently viable.
