# R18 Stage 2b: SG-LP Execution Review

**Date**: 2026-03-26
**Reviewer**: Execution Agent
**Artifacts reviewed**:
- `docs/alpha-research/round18_stage2b_sglp_backtest.md`
- `research/alphas/spread_gated_lp/impl.py`
- `research/alphas/spread_gated_lp/backtest.py`
- `src/hft_platform/strategies/opportunistic_mm.py`
- `config/base/strategies.yaml`
- `config/base/strategy_limits.yaml`

---

## VERDICT: CONDITIONAL APPROVE

The SG-LP backtest demonstrates a structurally sound spread-capture mechanism, but the reported numbers (+38,470 NTD/day) are overstated due to zero latency modeling and an optimistic P&L computation. After latency degradation and P&L correction, the strategy likely remains profitable but at 40-60% of reported levels. The production `OpportunisticMM` can be used with minimal config changes. Two conditions must be met before shadow deployment.

**Conditions**:
1. Re-run backtest with 36ms latency penalty (BLOCKER below)
2. Validate the per-fill P&L model against actual roundtrip economics

---

## 1. Feature Index Mapping — PASS (no drift)

### Production OpportunisticMM feature indices vs registry

| Constant in `opportunistic_mm.py` | Value | Registry `lob_shared_v2` index | Feature ID | Match? |
|---|---|---|---|---|
| `_IDX_OFI_DEPTH_NORM_PPM` | 16 | [16] | `ofi_depth_norm_ppm` | CORRECT |
| `_IDX_RET_AUTOCOV_5S_X1E6` | 17 | [17] | `ret_autocov_5s_x1e6` | CORRECT |
| `_IDX_TOB_SURVIVAL_MS` | 18 | [18] | `tob_survival_ms` | CORRECT |
| `_IDX_L1_BID_QTY` | 8 | [8] | `l1_bid_qty` | CORRECT |
| `_IDX_L1_ASK_QTY` | 9 | [9] | `l1_ask_qty` | CORRECT |

### Prototype vs production feature usage

The research prototype (`impl.py`) operates on raw `bid_px/ask_px/bid_qty/ask_qty` from numpy arrays and does **not** use the FeatureEngine at all. This is acceptable for research (direct data access), but means the prototype does not validate feature-index wiring. The production `OpportunisticMM` reads `spread_scaled` from `LOBStatsEvent` (not from the feature tuple) for the spread gate — this is the correct path.

**No feature index drift detected. Config drift = 0.**

---

## 2. Latency Impact — CRITICAL CONCERN

### Spread regime transition timing

From Stage 2a data:
- TMFD6 tick cadence: ~550ms median inter-tick
- Spread >= 5 occupancy: 57% of time
- Place RTT: 36ms, Modify RTT: 43ms

**Regime entry delay**: When spread widens from 4 to 5+ pts, our order arrives 36ms later. At 550ms tick cadence, this is 36/550 = **6.5% of a tick interval**. Most wide-spread episodes last multiple ticks (seconds to minutes based on 57% occupancy across the day), so the entry delay is small relative to episode duration.

**Regime exit risk**: When spread tightens from 5 to 4 pts, our cancel arrives 47ms later. During these 47ms, a pending order could get adversely filled at a now-unprofitable spread. This is the primary latency risk.

### Fill count degradation model

The backtest reports 872 fills/session (SG=5, OBI=0.0) with **zero latency**. In reality:

1. **Order posting delay** (36ms): Some short wide-spread episodes will end before our order arrives. Episodes shorter than 36ms are completely missed.

2. **Stale quote risk** (43ms modify cycle): During fast spread changes, our order sits at a stale price for up to 43ms. Fills during this window may be at unfavorable prices.

3. **Cancel delay** (47ms): When regime tightens, our order remains live for 47ms after we detect the change. Fills in this window may be adverse.

**Conservative degradation estimate**:
- Short-episode miss rate: ~10-15% of fills occur in brief spread episodes (< 1s). Of these, ~50% may be missed due to 36ms posting delay. Loss: 5-8% of total fills.
- Stale-quote adverse fills: ~5-10% of fills occur during transitions where a 43ms delay could worsen the fill. P&L degradation: 1-2 pts on affected fills.
- Cancel-delay adverse fills: ~3-5% of fills could occur after spread tightens below gate. These fills have negative expected P&L (gross < fee).

**Realistic fill count**: 872 × 0.85 ≈ **740 fills/session** (15% haircut)
**Realistic P&L/fill**: +4.41 × 0.85 ≈ **+3.75 pts/fill** (accounting for adverse stale fills)
**Realistic daily P&L**: 740 × 3.75 × 10 = **+27,750 NTD/day** (~72% of reported)

This is still substantially profitable. The strategy's edge comes from spread capture at wide spreads (20+ pts avg at eligible times), where the 36ms latency is negligible relative to the spread width.

---

## 3. Config-to-Research Consistency — PASS (no drift)

### strategies.yaml vs backtest parameters

| Parameter | `strategies.yaml` (OPPORTUNISTIC_MM_TMFD6) | Backtest (SG=5, OBI=0.0) | Match? |
|-----------|---|---|---|
| `spread_threshold_pts` | 5 | `spread_gate_pts=5` | MATCH |
| `tick_size_ratio_pct` | 50 | N/A (not used in prototype) | N/A |
| Symbol | TMFD6 | TMFD6 | MATCH |
| Position cap | 1 (from `strategy_limits.yaml`) | `max_position=1` | MATCH |

### strategy_limits.yaml vs backtest

| Parameter | `strategy_limits.yaml` | Backtest | Match? |
|-----------|---|---|---|
| `OPPORTUNISTIC_MM_TMFD6.max_position` | 1 | 1 | MATCH |
| `OPPORTUNISTIC_MM_TMFD6.max_order_qty` | 1 | 1 (implicit) | MATCH |
| `global_defaults.max_position_lots` | 4 | N/A | N/A |
| `global_limits.max_order_size` | 1 | 1 | MATCH |

### Fee model

| Parameter | Backtest | TMFD6 actual | Match? |
|-----------|---|---|---|
| Fee per leg | 2.0 pts (20 NTD) | Tax 7 NTD + Comm 13 NTD = 20 NTD per side | MATCH |
| RT fee | 4.0 pts | 40 NTD = 4 pts | MATCH |
| Point value | 10 NTD | 10 NTD (Mini-TAIEX) | MATCH |

**Config drift = 0.**

---

## 4. Risk Limits — PASS with one concern

### Position cap enforcement

- `strategy_limits.yaml`: `OPPORTUNISTIC_MM_TMFD6.max_position = 1`
- `PositionLimitValidator` in `risk/validators.py` (line 148): checks `abs(qty) > max_position_lots` before order acceptance
- Prototype enforces `max_position=1` internally via `should_quote_bid()/should_quote_ask()` checks

### Simultaneous fill race condition

**Concern**: The prototype allows both a pending bid and pending ask simultaneously (`pending_bid` and `pending_ask` are independent). If position = 0, bid fills (position -> +1) then ask fills (position -> -1). This is fine — position oscillates [-1, 0, +1].

BUT in production with 36ms RTT: if both sides fill within the same 36ms window (before we can cancel the other side), position could briefly reach +1 then immediately -1, or overshoot to +2 if the risk check on the second fill doesn't see the first fill yet.

**Mitigation already in place**: `PositionLimitValidator` checks position at order submission time, not fill time. Since `max_order_qty = 1` and `max_position = 1`, the risk engine will reject any new order when position is already at +/-1. The race condition is: fill arrives, position goes to +1, but the opposite-side order was already submitted (before the fill). The risk engine already approved it. If that order also fills, position goes to 0 (net flat) — this is actually safe.

**Worst case**: Both sides fill simultaneously from position=0. Position goes to +1 (buy fill) then immediately -1 (sell fill, net = 0). This is economically equivalent to a roundtrip — no excess risk. The prototype correctly models this: position bounds are [-1, +1] and the strategy won't post new orders beyond the cap.

**Verdict**: No risk gap. The 1-lot constraint + single order per side makes the race condition benign.

### HALT auto-cancel

`order/halt_canceller.py` provides `cancel_all_live_orders()` which is triggered by StormGuard HALT via `on_halt_callback`. This will correctly cancel outstanding SG-LP limit orders. The `OpportunisticMM.on_stats()` method also explicitly cancels `_bid_oid` and `_ask_oid` when spread tightens — the same pattern would apply on HALT (spread gate blocks, cancels fire).

---

## 5. Infrastructure Readiness — PASS (config change only)

### Can SG-LP run as OpportunisticMM config?

**YES.** The existing `OpportunisticMM` class already implements:
- Spread gate via `spread_threshold_pts` parameter (line 88)
- Spread-scaled integer comparison (line 209): `event.spread_scaled < self._spread_threshold_scaled`
- Cancel on spread tighten (lines 212-217)
- Delegate to `SimpleMarketMaker.on_stats()` for quoting when gate passes

The SG-LP backtest's best config (SG=5, OBI=0.0) maps directly to the **existing** `OPPORTUNISTIC_MM_TMFD6` entry in `strategies.yaml`:
```yaml
spread_threshold_pts: 5
```

The `reversal_filter_enabled` defaults to `False`, which matches the OBI=0.0 (no OBI filtering) backtest config.

**No new code needed for the primary config.** The `OpportunisticMM` is already deployed on TMFD6 with the exact parameters that match the best backtest config.

### What's different between prototype and production

| Aspect | Prototype (`impl.py`) | Production (`OpportunisticMM`) |
|--------|---|---|
| Fill detection | Queue depletion simulation | Broker fill callback (real) |
| Order posting | Instant (no latency) | 36ms broker RTT |
| Cancel | Instant | 47ms broker RTT |
| Quoting logic | Post at touch when spread >= gate | Delegate to `SimpleMarketMaker` (microprice-adjusted, inventory-skewed) |
| OBI filtering | Explicit `obi_threshold` | Via `reversal_filter_enabled` (different mechanism) |
| Position tracking | Internal counter | `RustPositionTracker` + risk engine |

The production `OpportunisticMM` quotes via `SimpleMarketMaker.on_stats()` which computes a microprice-adjusted fair value with inventory skew. This is **more sophisticated** than the prototype's simple "post at touch" — it may produce slightly different quote placement. However, the spread gate behavior is identical.

---

## 6. Operational Risk — MODERATE

### Opening session concentration

The backtest does not break down P&L by time-of-day, but Stage 2a data shows:
- 08:45-09:15 opening period has the widest spreads (many 20-99 pt events)
- Wide-spread 20+ pts bucket contributes disproportionately to P&L (+22.61 pts/fill vs +2.59 at 5-6 pts)

**Risk**: If the opening 30 minutes contribute 40%+ of daily P&L (plausible given spread distribution), then:

1. **Feed gap at open**: If Shioaji feed takes 1-2 minutes to stabilize at 08:45, we miss the widest spreads. StormGuard `FEED_GAP_HALT_S = 30s` threshold means a 30s gap triggers HALT, which blocks quoting.

2. **Login delay**: If session login takes longer than expected at 08:30, we may miss the 08:45 open entirely.

3. **StormGuard STORM**: Early-session volatility could trigger STORM state, blocking quoting during the most profitable window.

**Mitigation**: The `OpportunisticMM` config does not have a session gate — it quotes whenever spread >= 5 during any trading hours. Unlike CBS (which excludes opening 30 min), SG-LP captures the opening window. The risk is that THIS is where most of the edge lives.

**Recommendation**: After shadow deployment, measure the percentage of daily P&L from 08:45-09:15. If > 50%, the strategy has fragile time-concentration risk and should not be promoted beyond shadow without understanding opening reliability.

---

## 7. P&L Model Concerns — WARNING

### Per-fill P&L computation review

The backtest computes per-fill P&L as:
```python
net_pnl = gross_capture + pnl_5s - FEE_PER_LEG_PTS
```

Where:
- `gross_capture` = distance from fill price to mid at fill time (half-spread capture)
- `pnl_5s` = mid-price drift 5s after fill (adverse selection component)
- `FEE_PER_LEG_PTS` = 2.0 (one leg of roundtrip)

**This is correct** for a strategy where each fill is one leg of a roundtrip. A buy fill at the bid captures `(mid - bid)` and pays 2 pts fee. A sell fill at the ask captures `(ask - mid)` and pays 2 pts fee. Over a roundtrip (buy + sell), total capture = spread, total fee = 4 pts.

### Concern: 5s drift as adverse selection measure

The backtest uses 5s post-fill mid-price change as the adverse selection proxy. This is a **per-fill** mark-to-market, not a realized P&L. The actual realized P&L depends on when the opposite leg fills and at what price.

**Example**: Buy fill at 32,247, mid = 32,249 at fill time. 5s later mid = 32,245. The backtest records pnl_5s = -4 pts. But the sell fill might happen at 32,250 (if the ask was wide), giving an actual better exit. Or it might happen 30s later when mid is 32,240, giving a worse exit.

The 5s mark-to-market is a reasonable proxy but introduces noise. **The aggregate numbers are likely directionally correct but individual fill P&Ls have significant measurement error.**

### Concern: 872 fills/session — is this plausible?

At TMFD6 1.8 ticks/sec × 300 min = 32,400 total ticks. With 57% wide-spread: ~18,500 eligible ticks. 872/18,500 = 4.7% fill rate per eligible tick.

The prototype fills when queue depletes to our position (back of queue at ~4 lots depth). Queue depletion events should occur at roughly the tick arrival rate on the opposite side. With 1.8 ticks/sec and 2 sides, that's ~0.9 fills/sec × 300 min × 57% = ~9,234 potential fill events. At 4-lot queue, ~1 in 4 is back-of-queue: ~2,300 potential fills. 872 fills is 38% of that — plausible with the position cap and cancel behavior.

However, the prototype's queue tracking is simplified (it tracks `queue_ahead` as a single counter and decrements it when queue shrinks). Real queue behavior involves partial fills, order modifications, and new arrivals. The 872 number could be **overstated by 20-40%** due to fill detection optimism.

---

## Summary

| Check | Result | Notes |
|-------|--------|-------|
| Feature index mapping | **PASS** | All 5 indices match registry. No drift. |
| Latency impact | **WARNING** | ~15% fill count haircut, ~15% P&L/fill haircut → ~28K NTD/day (vs 38K reported) |
| Config consistency | **PASS** | Config drift = 0. Strategies.yaml, limits, fees all match. |
| Risk limits | **PASS** | 1-lot cap enforced. Race condition is benign. HALT cancel exists. |
| Infrastructure readiness | **PASS** | OpportunisticMM on TMFD6 already configured with SG=5. No new code needed. |
| Operational risk | **MODERATE** | Opening session concentration risk. Measure after shadow. |
| P&L model | **WARNING** | 5s mark-to-market proxy, fill count may be overstated 20-40% |

### Realistic P&L estimate after adjustments

| Metric | Reported | Latency-adjusted | Fill-optimism-adjusted | Conservative estimate |
|--------|----------|-----------------|----------------------|---------------------|
| Fills/session | 872 | 740 (-15%) | 610 (-30%) | 610 |
| P&L/fill | +4.41 pts | +3.75 pts (-15%) | +3.75 pts | +3.75 pts |
| Daily NTD | +38,470 | +27,750 | +22,875 | **+22,875** |

Even at the conservative estimate, **+22,875 NTD/day is substantially profitable** (Sharpe likely > 3 annualized with this fill count). The strategy warrants shadow deployment.

### Blockers before shadow

1. **[BLOCKER-E6] Latency-degraded backtest**: Re-run backtest with 36ms order posting delay and 47ms cancel delay. If P&L/fill drops below +1.5 pts (breakeven buffer), REJECT.

2. **[RECOMMENDED] Per-day P&L variance**: Report P&L for each of the 6 days individually. If any OOS day is negative, the strategy has day-level instability that needs investigation.
