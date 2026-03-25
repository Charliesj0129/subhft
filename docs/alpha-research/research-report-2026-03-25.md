# Alpha Research Report — 2026-03-25

## Research Rounds Summary

Two research rounds conducted in this session:
1. **Round 12**: Informed Trading Toxicity & VPIN (知情交易毒性與 VPIN 模型)
2. **Round 13**: MM Strategy Improvement (做市策略改善)

---

## Round 12: VPIN & Drift Burst

### Direction A: `vpin_regime_switch`

**Objective**: Real-time VPIN with 3-state regime detector (LOW/ELEVATED/TOXIC) for position-sizing.

**Implementation**:
- Research: `research/alphas/vpin_regime_switch/impl.py` (48 tests)
- Platform: `src/hft_platform/strategies/vpin_regime_switch.py` (41 tests, 94% coverage)
- Auto-calibration via P75/P95 percentiles after warmup
- Dual mode: tick-volume + depth-churn proxy

**Real Data Results** (TXFD6, 4 days, 1.55M rows):
| Metric | Value |
|--------|-------|
| C1: VPIN vs \|returns\| correlation | 0.438 (PASS < 0.85) |
| Regime distribution (calibrated) | LOW 43.6%, ELEVATED 16.5%, TOXIC 39.8% |
| Regime-conditional vol ordering | CORRECT (TOXIC > ELEVATED > LOW) |
| Standalone Sharpe | -0.14 (no standalone alpha) |
| **MM overlay: DD reduction** | **-30.6%** |
| MM overlay: PnL improvement | +48,080 NTD |

**Verdict**: Not an alpha. Effective as risk overlay for position-sizing.

### Direction B: `toxicity_drift_burst` (StormGuard Enhancement)

**Objective**: Detect drift bursts for StormGuard escalation.

**Implementation**:
- `src/hft_platform/risk/drift_burst_detector.py` (30 tests, 98% coverage)
- Integrated into `src/hft_platform/risk/storm_guard.py` (additive safety)
- Runtime wiring in `services/system.py` + `services/bootstrap.py`
- Env: `HFT_STORMGUARD_DRIFT_BURST_ENABLED=1`

**Real Data Results** (post-fix, TXFD6, 4 days):
| Metric | Before Fix | After Fix |
|--------|-----------|-----------|
| Bursts/day | 3,291 | 53.4 |
| StormGuard HALT | 11.6% | 0.02% |
| T-stat max | 19,183 | 38.4 |

**Critical fixes applied**: skip zero returns, min BPV floor, 5s cooldown default.

**Verdict**: Production-ready as StormGuard safety layer. Disabled by default.

---

## Round 13: MM Strategy Improvement

### Investigation Sequence

1. **P0: Quadratic Inventory Aversion** → L1 backtest
2. **P0 Parameter Sweep** → 168 combinations
3. **Latency Bug Discovery** → LATENCY_TICKS=500 was 500x too high (62.5s instead of 125ms)
4. **P0 v2 with corrected latency** → All negative Sharpe
5. **P2-lite: Selective Quoting** → IS Sharpe +3.80
6. **Walk-Forward Validation** → OOS FAIL (regime-dependent)
7. **L2 Queue-Aware Backtest** → Structural adverse selection confirmed

### Key Results

| Strategy | Best Sharpe | Data | Verdict |
|----------|-----------|------|---------|
| P0 Quadratic (L1, wrong latency) | +0.23 (fake) | Synthetic | Bug: 62.5s latency |
| P0 Quadratic (L1, corrected) | -112 to -207 | Real TXFD6 | FAIL |
| P2-lite Selective (L1, IS) | **+3.80** | Real TXFD6 | PASS (IS only) |
| P2-lite Selective (L1, OOS) | -9.8 to -25.7 | Real TXFD6 | **OOS FAIL** |
| L2 Queue-Aware (all strategies) | -100 to -125 | Real TXFD6 L2 | FAIL |

### Root Cause Analysis

**Why naive MM fails on TXFD6 at 36ms RTT**:

1. **Adverse selection is structural**: When a limit order gets filled, the market has by definition moved through the quote price. Average adverse price movement (~2.8 pts) exceeds spread capture (~2 pts).

2. **Queue priority is the core of MM profitability**: Queue-front MMs capture spread before the market moves against them. At 36ms RTT, we are always queue-back.

3. **P2-lite's IS success was regime-dependent**: Only profitable in tight-spread (1-5 tick) environments. January data had 200+ tick spreads where the strategy produced zero trades.

4. **Latency discovery**: Median TXFD6 tick interval is 125ms (3.7 ticks/sec), not sub-ms. 36ms RTT = less than 1 tick of delay.

---

## Structural Findings

### What Works
| Component | Evidence | Production Status |
|-----------|----------|-------------------|
| VPIN regime detection | Calibration works, vol ordering correct | Platform strategy ready |
| VPIN as MM DD overlay | -30.6% max drawdown reduction | Needs profitable base strategy |
| DriftBurstDetector | 53 bursts/day, HALT 0.02% | StormGuard integrated, disabled by default |
| OFI selective quoting | Sharpe +3.80 in tight-spread regime | Regime-dependent, not robust |

### What Doesn't Work
| Approach | Evidence | Root Cause |
|----------|----------|------------|
| Bidirectional MM on L1 | Sharpe -112 to -244 | No queue priority model |
| Bidirectional MM on L2 | Sharpe -100 to -125 | Queue-back adverse selection |
| Quadratic inventory penalty | No improvement over linear | Spread/fill model dominates |
| VPIN position limits | Worse than no VPIN | Forced flattens at bad prices |
| Multi-alpha composite | Sharpe -16 | Over-trading from noisy signals |

### Critical Learnings

1. **Backtest latency must match real tick frequency**: Always compute `latency_ticks = RTT_ms / median_tick_interval_ms` from actual data.

2. **MM profitability requires queue priority**: Without <5ms latency or co-location, pure MM on liquid futures is structurally unprofitable.

3. **VPIN's value is risk management, not alpha**: DD reduction is real, but requires a profitable base strategy to overlay.

4. **Walk-forward validation is essential**: P2-lite's IS Sharpe +3.80 completely collapsed OOS due to spread regime changes.

5. **L1 vs L2 fill models give opposite conclusions**: L1 mid-cross is too generous; L2 queue-back is too pessimistic. Reality is in between, depending on queue position.

---

## Deliverables

### Code (Committed)
| File | Purpose | Tests |
|------|---------|-------|
| `src/hft_platform/strategies/vpin_regime_switch.py` | VPIN platform strategy | 41 pass |
| `src/hft_platform/risk/drift_burst_detector.py` | Drift burst detector | 30 pass |
| `src/hft_platform/risk/storm_guard.py` | StormGuard integration | 14+96 pass |
| `research/alphas/vpin_regime_switch/impl.py` | VPIN research prototype | 48 pass |
| 11 backtest scripts in `research/tools/` | Full MM research suite | — |

### Data Exported
| File | Description |
|------|-------------|
| `research/data/raw/txfd6/TXFD6_all_l1.npy` | 6.3M rows, 12 days L1 |
| `research/data/raw/txfd6/TXFD6_*_l2.hftbt.npz` | 16.6M events, 4 days L2 |

---

## Recommended Next Steps

| Priority | Direction | Rationale |
|----------|-----------|-----------|
| P0 | **MXFD6 (小台指) MM** | Lower liquidity → less queue competition → possible queue-front |
| P1 | **Conditional P2-lite** | Only activate in tight-spread regime (spread < 10 pts) |
| P2 | **Cross-product lead-lag** | TXFD6 vs MXFD6 pricing divergence signals |
| P3 | **Latency reduction** | Evaluate co-location / direct API for <5ms RTT |
| P4 | **Event-driven trading** | Trade only on drift bursts / regime transitions |
