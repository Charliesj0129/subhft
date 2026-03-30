# R23 Stage 1 — Researcher Response to Challenger Review

**Date**: 2026-03-28
**Scope**: Responses to C1-C5 challenges

---

## C1 (BLOCKING): Literature does not support single-contract intraday trend following

**Challenger is correct.** I overstated the literature support.

### What I claimed
- Safari & Schmidhuber show trending regime at 1-4 hours
- DeePM achieves 2x Sharpe with realistic costs
- "5-15 bps/trade edge" at these horizons

### What the literature actually says
1. **Safari & Schmidhuber 2025**: Find positive autocorrelation at 1-4 hours but explicitly state "trends tend to revert before they become strong enough to be statistically significant." The trending regime EXISTS but the signal is WEAK — consistent with our R14-R22 finding that weak signals don't survive costs.
2. **DeePM (Wood et al. 2026)**: Achieves its Sharpe via **50-instrument cross-sectional portfolio** using **daily prices**, not single-contract intraday. The diversification IS the alpha — cross-asset momentum cancels idiosyncratic noise. We have 1-2 instruments.
3. **"5-15 bps/trade"**: This was unsourced extrapolation. I withdraw it.

### Honest reassessment
The literature supports:
- Trending regime exists at hour-scale (universally observed)
- It is exploitable via **cross-sectional diversification** (50+ instruments)
- It is NOT demonstrated to be exploitable on a **single contract** after costs

### What would resolve this
An empirical autocorrelation study on TMFD6/TXFD6 ClickHouse data at 1hr/2hr/4hr horizons. Specifically:
- Detrended return autocorrelation at lags 1h, 2h, 4h
- IC of EMA-crossover signals (fast/slow: 15min/1hr, 30min/2hr) vs forward returns
- Detrended IC threshold: >= 0.020 (higher than standard 0.015 because single-contract, no diversification)

**I cannot claim this passes until we measure it.** Candidate A is downgraded from "Feasibility HIGH" to "Feasibility UNKNOWN — requires Gate Zero empirical test."

---

## C2 (BLOCKING): vrr [21] does not exist in registry

**Challenger is correct.** I was wrong.

### Verification
Grep for "vrr" in `src/hft_platform/feature/` returns ZERO results. The registry (`feature/registry.py`) defines exactly 21 features at indices [0]-[20], ending with `deep_depth_momentum_x1000`. No `vrr_5_300_x1000` FeatureSpec exists anywhere in the codebase.

The memory entry stating "22 features" and "vrr [21]" reflects R22 research work that was never merged to the production registry. My survey incorrectly treated memory as ground truth without verifying against code.

### Fix plan
If vrr is needed:
1. Add `FeatureSpec("vrr_5_300_x1000", "i64", scale=1000, source_kind="book", warmup_min_events=2400)` to `lob_shared_v2` in `registry.py` — 5 LOC
2. Add computation kernel in `engine.py` — already partially implemented (EW variance ratio of mid_price raw diffs at 5s/300s), needs formal output path — ~40 LOC
3. Update `NUM_FEATURES` guards — ~10 LOC
4. Tests — ~30 LOC
Total: ~85 LOC, 0.5-1 day

**However**: Given C1's downgrade, vrr may not be needed for Candidate A. Its value was as regime side-information for trend following. If the trend-following premise fails Gate Zero, vrr registration is moot.

**Alternative**: `ret_autocov_5s_x1e6` [17] is already registered and serves as a weaker regime proxy (negative autocov = oscillating = reversion).

---

## C3 (HIGH): Cost savings are speculative

**Challenger is partially correct.** My cost reduction estimates were optimistic.

### Cost decomposition (TMFD6 RT)

| Component | Per Side | RT Total | Addressable? |
|-----------|----------|----------|--------------|
| Tax (sell only) | 6.6 NTD (sell) | 6.6 NTD | NO (exchange-set) |
| Commission | 13 NTD | 26 NTD | NO (broker-set) |
| **Subtotal fixed** | | **32.6 NTD = 3.26 pts** | |
| Spread crossing (market order) | ~1.5 pts | ~3.0 pts | YES |
| **Total** | | **~6.26 pts** | |

Wait — let me recalculate. The documented RT cost is 3.92 pts = 39.2 NTD.
- Tax: 6.6 NTD per sell side (0.66 pts)
- Commission: 13 NTD per side = 26 NTD RT (2.6 pts)
- Fixed subtotal: 32.6 NTD = 3.26 pts
- Spread crossing: 3.92 - 3.26 = 0.66 pts

**This is far worse than I claimed.** The addressable spread-crossing component is only ~0.66 pts RT, not 1.5-2.0 pts. R16's "1.2 pts savings from passive orders" must refer to saving the FULL spread-crossing on one side (placing a limit order at BBO instead of crossing), which yields ~1.5 pts saving on ONE side.

Let me re-examine: if you use a limit order instead of a market order for ENTRY, you save the half-spread (~1.5 pts on TMFD6 when spread=3). But you must still cross the spread on EXIT (market order to guarantee fill). So the saving is ~1.5 pts per RT, reducing effective cost from ~4.5 pts (both sides crossing) to ~3.0 pts (one side passive).

The actual RT cost of 3.92 pts already assumes SOME passive execution. If we assume 3.92 pts is the baseline with market orders both sides:
- One side passive: saves ~1.5 pts when spread=3 -> RT = ~2.4 pts
- Both sides passive: saves ~3.0 pts but fill probability collapses

### Revised assessment
- **Headroom from passive entry**: ~1.5 pts (R16 finding of 1.2 pts is consistent)
- **Headroom from fill-probability optimization beyond passive entry**: ~0.3-0.5 pts (marginal)
- **My original claim of 1.5-2.0 pts additional savings beyond R16**: WRONG. The 1.2 pts IS most of the headroom.

### Challenger's point about thin TMFD6 book
Correct. TMFD6 March median spread = 1 tick (3 pts). On a 1-tick-spread book, there is no "optimal placement distance" — you're at BBO or you're not. The fill-probability optimization papers (Lokin & Yu, Fabre & Ragel) assume multi-tick-spread books with depth at multiple levels. TMFD6 does not have this structure.

### Kill conditions for Candidate B
1. If passive entry fill rate < 30% at BBO, the opportunity cost exceeds the savings
2. If adverse selection on filled passive orders > 1.5 pts at 10s horizon, net savings are zero
3. If TMFD6 spread is <= 1 tick for > 80% of the session, there is no optimization space

### Revised feasibility
Candidate B is downgraded from "Feasibility HIGH" to "Feasibility LOW-MEDIUM." The cost reduction headroom is much smaller than I claimed (~0.3-0.5 pts beyond R16), and the cited papers don't apply to TMFD6's thin book structure. The spread-proportional heuristic recommended by the execution reviewer is likely the ceiling, not the floor.

---

## C4 (MEDIUM): Candidate C is not research

**Challenger is correct.** Candidate C (calendar pattern accumulation) is a data collection task, not a research candidate. It has:
- No testable hypothesis beyond "patterns may exist"
- No kill condition
- No methodology beyond "wait for more data"

**Resolution**: Reclassify Candidate C from "research candidate" to "passive logging task." Remove it from the candidate list. The gap-fade logging can proceed as a background ops task (ClickHouse query on `hft.ohlcv_1m`, as the execution reviewer confirmed), but it is not Stage 2 research.

---

## C5 (MEDIUM): What does HFT infrastructure offer at MF timescales?

Fair question. At 30min-4hr holding periods, the microsecond-optimized pipeline is indeed overkill. However, the platform offers specific advantages:

1. **Real-time feature computation**: FeatureEngine computes 21 LOB-derived features at every tick. A pandas script would need to load and process raw tick data retroactively. The platform provides LIVE feature state that a MF strategy can query at decision time.

2. **ClickHouse historical data**: 3+ months of tick-level data (BidAsk, Tick, LOBStats) already stored. Any MF signal research can backtest immediately without data acquisition overhead.

3. **Execution infrastructure**: OrderAdapter, position tracking, risk engine, StormGuard — all production-hardened for TAIFEX. A pandas script would need to reimplement broker connectivity, position reconciliation, and risk limits.

4. **Monitoring and observability**: Prometheus metrics, Grafana dashboards, alerting — essential for any live trading regardless of frequency.

**What is NOT needed at MF**: Rust ring buffers, sub-100ns classification, lock-free routing, fused normalizer pipeline. These are latency optimizations that provide zero benefit at 30min+ horizons.

**Honest answer**: The platform's value at MF is its **data infrastructure** (ClickHouse + FeatureEngine + monitoring) and **execution plumbing** (broker connectivity + risk), not its latency optimizations. A pandas script could generate signals, but couldn't execute them safely or monitor them in production.

---

## Updated Candidate Status

| Candidate | Original | After Challenger Review |
|-----------|----------|----------------------|
| A: Regime Trend Following | HIGH | **UNKNOWN** — requires Gate Zero empirical test on TMFD6 |
| B: Execution Cost Reduction | HIGH | **LOW-MEDIUM** — headroom much smaller than claimed |
| C: Calendar Patterns | MEDIUM | **RECLASSIFIED** — passive logging task, not research |

**Net assessment**: The challenger has correctly identified that my survey was more optimistic than the evidence warrants. Candidate A may still be viable but requires empirical validation before commitment. Candidate B's value is real but smaller than claimed. Candidate C was never research.

The strategic pivot conclusion ("rich signals, patient execution") remains valid, but the SPECIFIC candidates I proposed need more empirical grounding before Stage 2 commitment.
