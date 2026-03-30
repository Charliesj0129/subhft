# Gate-Zero Execution Review: OFI Persistence Results

**Reviewer**: Execution Reviewer
**Date**: 2026-03-26
**Artifact**: `docs/alpha-research/round18/gate_zero_ofi_persistence.md`

---

## Question 1: Does r=0.102 at 5min translate to enough edge?

### Correlation-to-IC conversion

Predictive correlation r and information coefficient IC are closely related. For a linear signal,
IC ~ r when both are measured as rank or Pearson correlation between signal and forward returns.
So r=0.102 implies IC ~ 0.10.

### IC to expected edge

Using the fundamental law approximation:

```
E[return per trade] ~ IC * volatility_per_period * transfer_coefficient
```

TMFD6 5-minute return volatility (typical for Taiwan mini futures):
- Assume annualized vol ~ 15-20% for TAIEX
- 5-min vol ~ annual_vol * sqrt(5 / (252 * 270)) = 0.175 * sqrt(5/68040) ~ 0.175 * 0.00857 ~ 0.0015 (15 bps)
- In points: mid_price ~ 21000, so 15 bps ~ 3.15 pts per 5-min period

Expected edge per trade:
```
E[edge] ~ IC * sigma = 0.102 * 3.15 pts ~ 0.32 pts per 5-min trade
```

**This is BELOW the 3.92 pts (or effectively 4 pts) RT cost.**

Even with perfect transfer coefficient (TC=1), the expected edge of ~0.32 pts is roughly **8% of RT cost**. The signal would need IC ~ 1.25 to break even as a taker strategy, which is impossible (IC is bounded by 1.0).

### Alternative: passive entry

If we use log-GOFI as a directional filter for a passive maker strategy (Direction B from the earlier survey), the cost is different. A passive fill at the touch earns half the spread instead of paying it. At spread >= 5 pts:
```
Passive RT cost = 2 * (tax + commission) = 2 * (6.6 + 13) = 39.2 NTD = 3.92 pts
```
Wait -- passive fills still pay tax and commission. The cost structure doesn't change between maker and taker on TMFD6 (no maker rebates). The 3.92 pts RT cost applies regardless.

The only way passive helps is if you earn the spread: you buy at bid and sell at ask, capturing spread - RT_cost. At spread = 5 pts: net = 5 - 3.92 = 1.08 pts. The directional signal then only needs to ensure you're not adversely selected (i.e., the fill doesn't move against you by more than 1.08 pts).

### Bottom line on edge

**As a standalone taker signal**: r=0.102 generates ~0.32 pts expected edge vs 3.92 pts cost. **INSUFFICIENT by 12x.** This is consistent with our R16/R17 finding that L1 microstructure signals cannot cover taker costs.

**As a maker signal filter**: r=0.102 could add value by conditioning WHEN to quote (reducing adverse selection by ~10%). This is the only viable path. The signal cannot be a standalone alpha.

---

## Question 2: What is the implied IC?

| Metric | Value |
|--------|-------|
| Predictive correlation (log-OFI, 5min, TMFD6) | r = 0.102 |
| p-value | 0.005 |
| N buckets | 773 |
| Implied IC | ~0.10 |
| IC at our R17 breakeven (30min) | 0.043 required |

The IC of 0.10 at 5min EXCEEDS the 30-min breakeven threshold of 0.043. However, this comparison is misleading:

1. The R17 breakeven IC of 0.043 assumes a **30-minute holding period** where price moves are larger (~6-10 pts). At 5-minute horizons, price moves are smaller (~3 pts), so the required IC is higher.

2. Correct breakeven calculation at 5min:
```
Required IC = RT_cost / (sigma_5min * sqrt(N_trades_efficiency))
            = 3.92 / 3.15 = 1.24  (for single trade, taker)
```
This confirms taker is unviable.

3. For the signal as a **filter** (not standalone), we need to think in terms of incremental Sharpe improvement rather than standalone breakeven. An IC of 0.10 is moderate -- it can meaningfully improve a strategy's hit rate by a few percentage points.

---

## Question 3: Signal half-life vs execution latency

- Signal horizon: peaks at 5 minutes (log-OFI) with significant predictive power at 1-5 min.
- Signal update frequency: computed over rolling 5-min windows, so it changes slowly (new value every few seconds at most).
- Execution latency: 36ms submit RTT.
- **Ratio: 5 min / 36 ms = 8,333x.** Latency is completely irrelevant at this horizon.

Even at the shortest significant horizon (30s, r=0.032), latency is 36ms / 30s = 0.12%. No latency concern whatsoever.

**Assessment: STRONG PASS.** This is the best latency-to-signal ratio we've seen in any research round.

---

## Question 4: Does TMFD6-only focus make sense?

### Why TMFD6 works and TXFD6 doesn't

| Factor | TMFD6 | TXFD6 |
|--------|-------|-------|
| Contemp logOFI (5min) | 0.748 | 0.493 |
| Predictive logOFI (5min) | 0.102 (p=0.005) | 0.062 (p=0.236) |
| L1 depth (typical) | ~4 lots | ~20-50 lots |
| Spread (median) | 3-7 pts | 1 pt |
| Tick rate | ~1.8/sec | ~8/sec |
| N buckets (5min) | 773 | 443 |

TMFD6's thinner book creates larger OFI-to-return impact. When 4 lots of OFI hits a 4-lot-deep book, the effect is proportionally much larger than 20 lots of OFI hitting a 50-lot-deep book. This is exactly the 1/(2D) relationship from Cont (2014) and Takahashi (2025).

### TMFD6-only is correct but has implications

1. **Liquidity constraint**: TMFD6 at 1 lot per trade, ~1.8 ticks/sec. At 5-min signal horizon with 1 lot sizing, maximum capacity is ~60 round-trips per day. At ~1 pt net edge per trade (optimistic, as a maker filter), that's ~60 pts/day = ~600 NTD/day. This is small but positive.

2. **Statistical power**: 773 five-minute buckets over 58 days is reasonable for initial validation. However, this is ~13 observations per day, and the effect is small. Need to be cautious about regime-dependence (the R16 finding that Jan/Feb wide-spread regime differs from March).

3. **TXFD6 deferred is correct**: p=0.236 at 5min means we cannot reject the null. The data does not support OFI persistence on TXFD6 at medium frequency. The report's suggestion to try full multi-level GOFI on TXFD6 is reasonable but speculative.

4. **Risk from TMFD6 concentration**: If the signal only works on TMFD6, we're adding another TMFD6-only strategy alongside CBS_TMFD6 and OpMM_TMFD6. The `max_position_lots=4` global limit and mutex groups become important. Three strategies competing for position on one thin instrument creates crowding risk within our own portfolio.

**Assessment: TMFD6 focus is empirically justified. But strategy crowding and capacity constraints are real concerns that Stage 2 must address.**

---

## Quantitative Summary

| Question | Answer | Implication |
|----------|--------|-------------|
| Edge vs cost (taker) | 0.32 pts vs 3.92 pts | **FAIL standalone** -- 12x shortfall |
| Edge vs cost (maker filter) | IC=0.10 improves hit rate ~5% | **VIABLE as filter** for spread-gated strategy |
| Implied IC | ~0.10 | Above 30min breakeven (0.043), below 5min taker breakeven (1.24) |
| Latency compatibility | 8,333x ratio | **STRONG PASS** |
| TMFD6 focus | Empirically correct | Capacity ~600 NTD/day, crowding risk with CBS/OpMM |

---

## Verdict: CONDITIONAL PASS with Scope Narrowing

The gate-zero results PASS the kill gate (r=0.102 > 0.05 at 5min, p=0.005) but reveal a critical scope constraint:

### What SURVIVES
- log-OFI as a **directional filter for passive maker strategies** on TMFD6
- Candidate A (log-GOFI): multi-level construction may further improve the 0.102 correlation
- Candidate B (OFI-OU): regime framework may identify periods where r >> 0.102

### What is DEAD
- Any standalone taker strategy based on OFI at 5-min horizon (edge 12x below cost)
- TXFD6 applications (insufficient statistical evidence)

### Mandatory Stage 2 constraints
1. **Scope**: log-GOFI and OFI-OU must be tested as FILTERS for OpMM/CBS, not as standalone directional alphas
2. **Integration**: Test log-OFI as an additional gate in existing OpMM_TMFD6 (suppress quoting when log-OFI predicts adverse direction)
3. **Regime split**: Report results separately for Jan/Feb (wide spread) vs March (normal spread) to check regime-dependence
4. **Capacity**: Model realistic trade frequency and daily PnL under 1-lot constraint
5. **Crowding**: If log-OFI filter is added to OpMM, check interaction with CBS_TMFD6 mutex group

---

## Supplementary: Implied Sharpe Ratio

For a standalone taker strategy trading once per 5-min bucket:
```
E[edge per trade] ~ 0.32 pts
sigma per trade ~ 3.15 pts (5-min vol)
Per-trade Sharpe ~ 0.32 / 3.15 = 0.10

Trades per day ~ 60 (one per 5-min bucket, 08:45-13:45 = 5 hrs)
Daily Sharpe ~ 0.10 * sqrt(60) = 0.77
Annualized Sharpe ~ 0.77 * sqrt(252) = 12.3
```

This looks deceptively good because of high trade frequency. But this calculation IGNORES costs:
```
E[edge after cost] = 0.32 - 3.92 = -3.60 pts per trade
Daily PnL = -3.60 * 60 = -216 pts/day = -2,160 NTD/day LOSS
```

As a **maker filter** (not standalone), the Sharpe calculation is different. If log-OFI reduces adverse selection rate by ~10% on OpMM fills (IC=0.10 contribution), and OpMM generates ~20 fills/day at spread >= 5 pts with avg net +1.08 pts/fill:
```
Without filter: 20 fills * 1.08 pts * (1 - adverse_rate)
With filter: same but adverse_rate reduced by ~10%
```

The incremental value depends on OpMM's baseline adverse selection rate, which is unknown (this is the key Stage 2 measurement).

---

## Supplementary: FeatureEngine Changes Required

### For Candidate A (log-GOFI)

Current FE v2 has `ofi_l1_raw` [11] (L1-only, linear). log-GOFI needs:

1. **New feature: `log_gofi_5m`** (tentatively index [21])
   - Multi-level OFI: track BBO traversals across L1-L5, aggregate depth changes at all traversed levels
   - Log stationarization: `sign(delta_q) * log(1 + |delta_q|)` per level
   - 5-minute rolling accumulator (not EMA -- discrete window sum)
   - Warmup: 5 minutes of data (~2,400 ticks at 8 ticks/sec TXFD6, ~540 ticks at 1.8/sec TMFD6)

2. **State requirements**:
   - Previous BidAskEvent snapshot (L5 prices + quantities) -- already maintained in LOBEngine
   - Rolling window buffer of per-tick GOFI contributions (~540 entries for 5min on TMFD6)
   - This is more state than any current FE feature (most are stateless or single EMA)

3. **Implementation path**:
   - Research prototype: compute from ClickHouse L5 data offline (~100 LOC Python)
   - Production: new FeatureEngine feature consuming BidAskEvent directly (~120 LOC in engine.py)
   - Rust kernel: optional but recommended if the 5-min rolling buffer becomes hot-path bottleneck
   - Schema bump: `lob_shared_v2` -> `lob_shared_v3` (or extend v2 with additive slot)

4. **Risk**: Adding a 5-minute rolling buffer to FE increases memory per symbol. At ~540 entries * 8 bytes = ~4.3 KB per symbol, this is negligible.

### For Candidate B (OFI-OU regime)

No FE changes needed for the core signal (uses existing `ofi_l1_raw` or `ofi_l1_ema8`). Regime detection and OU parameter fitting are strategy-level computations, not tick-level features. If a regime indicator is later promoted to FE, it would be:

1. **Possible future feature: `ofi_regime_efficiency`** (binary or continuous)
   - Rolling autocorrelation of OFI over ~50-100 buckets
   - Very slow-changing (updates every few minutes)
   - LOW priority for FE integration -- strategy-internal is fine for Stage 2

### Summary of FE impact

| Candidate | FE changes | Effort | Schema bump? |
|-----------|-----------|--------|-------------|
| A: log-GOFI | 1 new feature [21] + rolling buffer | ~120 LOC | Yes (v2 additive or v3) |
| B: OFI-OU | None for Stage 2 | 0 LOC | No |

---

*Execution Reviewer -- R18 Gate-Zero*
