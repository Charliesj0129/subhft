# Round 17 Stage 2: TSMC (2330) Lead-Lag — Execution Review

**Date**: 2026-03-26
**Reviewer**: Execution Agent
**Artifact**: `docs/alpha-research/round17_stage2_tsmc_leadlag_results.md`
**Prototype**: `research/experiments/validations/tsmc_leadlag/prototype_ic.py`

---

## Verdict: CONDITIONAL

Proceed to expanded data validation (20+ days) before any implementation work. The signal is promising but has critical data coverage and architecture gaps.

---

## 1. Platform Data Pipeline

### 2330 Subscription Status: ALREADY SUBSCRIBED

- `config/symbols.yaml:62` — 2330 (TSMC) is in the active symbol list as `product_type: stock`.
- `config/base/strategies.yaml` — No strategy currently consumes 2330 events. It is subscribed for recording only.
- 2330 L1 data (tick + bidask) is already flowing through the normalizer, LOB engine, and recorder pipeline. **No subscription changes needed.**

### Cross-Symbol Architecture: BLOCKING GAP

This is a **cross-symbol strategy** — it consumes 2330 market data to generate TMFD6 trading signals. The current architecture has no support for this pattern:

1. **FeatureEngine** (`src/hft_platform/feature/engine.py`) is **single-symbol per invocation**. It processes `LOBStatsEvent` and computes features keyed by `event.symbol`. There is no mechanism to inject a feature computed from symbol A into the feature vector of symbol B. No `cross_symbol`, `external`, `inject`, or `multi_symbol` patterns exist in the feature module.

2. **StrategyRunner** (`src/hft_platform/strategy/runner.py:496-599`) dispatches events to all enabled strategies. It does NOT filter by `strategy.symbols` — every strategy sees every event. CBS filters internally at `cascade_bounce.py:133` (`if symbol not in self._state`). So a TSMC lead-lag strategy on TMFD6 would receive 2330 events and could consume them — **but this is an implicit side-channel, not a designed interface**.

3. **Strategy `symbols` field** in `strategies.yaml` declares `["TMFD6"]` for CBS. A lead-lag strategy would need to declare `["TMFD6", "2330"]` or use a new `data_symbols` / `signal_symbols` distinction. The current schema conflates "symbols I trade" with "symbols I observe."

**Config drift assessment**: 0 (no config changes made). But implementation requires architecture work.

### Required Changes (Estimated)

| Change | Scope | Risk |
|--------|-------|------|
| Add `data_symbols` vs `trade_symbols` distinction to strategy config schema | Medium | Low |
| Ensure 2330 LOBStatsEvent reaches FeatureEngine for TMFD6 strategies | Medium | Medium — must not break single-symbol feature isolation |
| Cross-symbol feature injection in FeatureEngine (or bypass FE entirely) | Large | High — new architectural pattern |

---

## 2. Latency Budget: PASS (non-issue)

- Signal lookback: 60-300s. Signal horizon: 300-600s. This is a **slow signal** by HFT standards.
- At 36ms Shioaji RTT, the signal computation budget is effectively unlimited (seconds, not microseconds).
- 2330 tick rate is ~1-2/sec during day session. Computing a 60-300s rolling return is trivial — a single subtraction on a ring buffer.
- **No real-time urgency**: A 1-second polling loop on 2330 mid_price is sufficient. No need for tick-by-tick processing.
- The signal can be computed entirely in the strategy's `handle_event` method using a small ring buffer of 2330 mid_prices. No Rust kernel needed.

---

## 3. Feature Engine Integration: BYPASS RECOMMENDED

Given the FeatureEngine's single-symbol design, the pragmatic path is:

1. **Do NOT route this through FeatureEngine.** The FE is designed for per-symbol LOB features (spread, imbalance, OFI). A cross-symbol return signal is categorically different.

2. **Strategy-internal computation**: The lead-lag strategy maintains its own ring buffer of 2330 mid_price_x2 values (300 entries at 1/sec = 300 bytes). On each TMFD6 LOBStatsEvent, it reads the latest 2330 return from its internal buffer. This is the same pattern CBS uses for its price history.

3. **Future**: If cross-symbol features become common, design a proper `CrossSymbolFeatureEngine` or `ExternalSignalBus`. But for a single signal, strategy-internal is correct.

---

## 4. Strategy Architecture

### CBS Coexistence: COMPATIBLE

- CBS is **contrarian** (enter against large moves). TSMC lead-lag is **momentum** (enter with TSMC direction). These are orthogonal signals.
- CBS triggers on 40 bps moves in 600s — rare events (a few per day). TSMC lead-lag would fire on every 60-300s 2330 return exceeding a threshold — potentially many signals per day.
- **They will rarely conflict** because CBS requires an extreme move to trigger, while lead-lag operates on normal price drift.

### Position Sizing: NEEDS LIMITS

- `config/base/strategy_limits.yaml:12` — `max_position_lots: 3` globally (OpMM_TX + OpMM_TMF + CBS concurrent).
- CBS has `max_position: 1` on TMFD6. A new TSMC lead-lag strategy on TMFD6 would also need `max_position: 1`.
- **Simultaneous firing**: If both CBS and lead-lag hold TMFD6 positions, the combined position = 2 lots. This is within the global `max_position_lots: 3` limit.
- **Recommendation**: Add a per-symbol aggregate limit (e.g., `TMFD6_max_aggregate_position: 2`) to cap total TMFD6 exposure across strategies. Currently the risk engine validates per-strategy, not per-symbol-aggregate.

---

## 5. Cost Model Verification: MARGINAL — NEEDS MORE DATA

### Expected Edge Calculation

- Best pooled IC = 0.065 (LB=300s, H=600s). Usable IC = 0.044-0.065.
- IC-to-expected-return rough conversion: For H=300s on TMFD6, typical absolute return per bar is ~5-15 bps. IC=0.05 implies expected return per signal of approximately `IC * sigma_ret * sqrt(2/pi)` which at TMFD6 volatility gives roughly **3-8 bps per signal**.
- TMFD6 RT cost = 1.33 bps (4 pts at ~30K index, point_value=10 NTD, fee=40 NTD RT).
- **Net edge estimate: 1.7-6.7 bps per trade**. This is marginal at the low end and viable at the high end.
- Hit rate: 55.4-60.8% pooled. At 55% HR with 1:1 risk-reward, expected edge = `0.55 * R - 0.45 * R = 0.10 * R`. If average win = average loss = 8 bps, net = 0.8 bps per trade minus 1.33 bps cost = **negative**. The HR-based estimate is less favorable than the IC-based estimate.

### Realistic Fill Assumptions

- At H=300-600s, the strategy holds for 5-10 minutes. Market orders are acceptable (not queue-dependent).
- Slippage: TMFD6 median spread = 4 pts. Market order eats half-spread = 2 pts entry + 2 pts exit = 4 pts total = 1.33 bps. This is already in the cost model.
- **No additional adverse fill risk** at this horizon (unlike MM strategies).

### Verdict on Cost

The edge is **marginal but plausible** if the IC holds at 0.05+ on a larger sample. The 3-day IC estimate has wide confidence intervals. At IC=0.03 (lower bound of plausible range), the strategy is breakeven-to-negative after costs.

---

## 6. Data Gap: CRITICAL BLOCKER

### Current Data

- **npy files**: 3 overlapping days (Mar-20, 23, 24). Mar-23 is partial (2330 ends at 10:51).
- **ClickHouse**: 36 days of 2330 data available. The results document mentions an 8-hour timestamp offset issue.
- **Effective sample**: 2.5 days of aligned data, ~32K 1-second bars.

### Statistical Concerns

1. **3 days is insufficient for IC estimation.** Per-day IC ranges from -0.037 to +0.209. This variance means the pooled IC=0.065 has a confidence interval roughly +/- 0.10. The signal could easily be zero.
2. **LB=300s, H=300s, Mar-20 IC=0.209** is suspiciously high. At LB=H, there is significant overlap between the lookback and horizon windows of adjacent bars (299 of 300 seconds overlap). This inflates autocorrelation and IC. The prototype does not deoverlap or use non-overlapping windows.
3. **Mar-23 partial day** shows weak/mixed results. If the signal only works on full days, the effective sample is 2 days.

### Required Data Work

1. **Export 30+ days of aligned 2330 + TMFD6 data from ClickHouse** with corrected timestamps (UTC+8 offset fix).
2. **Re-run IC with non-overlapping windows** (sample every H seconds, not every 1 second) to eliminate autocorrelation inflation.
3. **Bootstrap confidence intervals** on pooled IC with the expanded dataset.
4. **Minimum threshold for implementation**: IC >= 0.03 with p < 0.01 on 20+ days, with consistent sign on >= 70% of days.

---

## 7. Prototype Quality

The prototype script (`prototype_ic.py`) is well-structured for research. Observations:

- **Correct**: Uses Spearman rank IC (robust to outliers), separates signal groups, reports per-day stability.
- **Concern**: Pooled IC is computed on concatenated data across day boundaries. This can introduce spurious cross-day correlations (e.g., Mar-20 close price vs Mar-23 open). The `compute_past_returns` will produce NaN at boundaries, but `compute_forward_returns` at end-of-day looks 600s into... nothing (NaN). This is handled by `dropna` in `spearman_ic`, so it is safe.
- **Missing**: No non-overlapping window analysis. No bootstrap CI. No transaction cost simulation.

---

## Summary of Conditions

| # | Condition | Type | Blocking? |
|---|-----------|------|-----------|
| C1 | Expand to 20+ days of aligned 2330+TMFD6 data (fix 8h offset) | Data | YES |
| C2 | Re-run IC with non-overlapping windows to remove autocorrelation inflation | Analysis | YES |
| C3 | Confirm IC >= 0.03 with p < 0.01 on expanded dataset | Statistical | YES |
| C4 | Design cross-symbol event routing (data_symbols vs trade_symbols) before implementation | Architecture | YES (for production) |
| C5 | Add per-symbol aggregate position limit for TMFD6 across strategies | Risk | YES (for production) |
| C6 | Strategy-internal 2330 ring buffer is acceptable for prototype; no FE integration needed | Architecture | Informational |

**Next gate**: Clear C1-C3 (data + statistical validation). If IC holds, proceed to C4-C5 (architecture) and shadow implementation.
