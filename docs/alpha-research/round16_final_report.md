# Round 16 Final Report: q-fin.TR Literature Exploration

**Date**: 2026-03-26
**Status**: CLOSED — All candidates empirically invalidated
**Instrument**: TMFD6 (微台指) / TXFD6 (大台)
**Cost basis**: XMT 40 NTD RT = 4.0 pts = 1.33 bps

---

## Executive Summary

Round 16 explored q-fin.TR literature (60+ papers) across two surveys to find profitable strategies for TMFD6 at retail cost structure. Six candidate directions were empirically tested on 9.16M TMFD6 ticks (20 days) and 1.78M TXFD6 ticks (4 days). **All failed.**

The fundamental finding: **there is no exploitable microstructure alpha on TMFD6 front-month at L1 level with retail cost structure (4.0 pts RT, no maker rebates).**

---

## Candidates Tested

### Survey V1 (q-fin.TR focused)

| # | Direction | Source Paper | Result |
|---|-----------|-------------|--------|
| 1 | Imbalance Reversal Detection | Albers et al. 2025 (arXiv:2502.18625) | FAIL — IC=0.04, 52% accuracy, cost-viable horizon (60s+) exceeds signal half-life (15s) |
| 2 | OFI Regime-Conditional | Hu & Zhang 2025 (arXiv:2505.17388) | REJECT — 0.001 bps directional ceiling (Round 14), even 100x regime amplification insufficient |
| 3 | Toxic Flow Detection | Cartea & Sanchez-Betancourt 2025 (arXiv:2503.18005) | FAIL — broker-centric model untranslatable to retail; +0.03 pts timing improvement (negligible) |

### Survey V2 (expanded scope)

| # | Direction | Source Paper | Result |
|---|-----------|-------------|--------|
| 4 | Push-Response Mean-Reversion | Vlasiuk & Smirnov 2025 (arXiv:2511.06177) | CONDITIONAL — works in wide-spread regime only (+116 pts reversion), March shows momentum (-35 pts) |
| 5 | Order-Flow Entropy | Singha 2025 (arXiv:2512.15720) | FAIL — Q1/Q5 ratio ≈ 1.0 (threshold: 2.0), trade direction inference too lossy from L1 snapshots |
| 6 | Spread Regime Prediction | He et al. 2024 (arXiv:2404.11722) | FAIL — AC=0.51 (threshold: 0.8), "regime" is contract maturity artifact, not predictable pattern |

### Additional tests

| Test | Result |
|------|--------|
| Spread-conditional maker (wide spread = opportunity?) | TRAP — adverse selection, median PnL = -8 pts/fill |
| Execution optimization (reversal timing) | +0.03 pts/trade (negligible vs unconditional passive) |
| Passive limit order execution | +1.2 pts/trade savings vs taker (ONLY actionable finding) |
| CatBoost multi-feature | REJECT — no trade records or L5 data exported, 58 days insufficient |
| Closing auction effects | REJECT — 1-2 trades/day, TMFD6 auction too thin |

---

## Key Empirical Findings

### Signal Properties on TMFD6

| Signal | IC (best) | Accuracy (best) | Half-life | Needed Accuracy (4.0 pts cost) |
|--------|----------|-----------------|-----------|-------------------------------|
| L1 depth imbalance | 0.040 (5s) | 52.0% | ~15s | 65-107% depending on horizon |
| OFI (unconditional) | 0.049 | 52.5% | ~10s | same |
| OFI (best quintile, low vol) | 0.076 | ~53% | ~15s | same |
| Push-response (March) | N/A | momentum, not reversion | N/A | N/A |

### Spread Regime Discovery

| Period | Median Spread | Bid/Ask Qty | Spread ≥ 4 pts | Signal Quality |
|--------|--------------|-------------|----------------|---------------|
| Jan 26 - Feb 10 | 28-43 pts | 1-40 | 94-100% | Strong (IC=0.19, reversion works) |
| Feb 23-25 | 6-7 pts | 1 | 59-94% | Moderate |
| Mar 19-26 | 3 pts | 3-4 | 1-15% | Dead (IC≈0, momentum not reversion) |

The wide-spread regime has genuine signals but is a structural/calendar effect (likely contract maturity), not a predictable intraday pattern.

### Cost Model Corrections

| Item | Previous Assumption | Corrected Value | Source |
|------|-------------------|-----------------|--------|
| XMT transaction tax | 2.0 bps sell-only | 7 NTD/side (both sides) | User-confirmed |
| XMT commission | ~1.5 bps/side | 13 NTD/side | User-confirmed |
| XMT RT total | 3.5 bps / 11.55 pts | **40 NTD = 4.0 pts = 1.33 bps** | User-confirmed |
| Tax side | sell-only | both sides | User-confirmed |
| Config `tax_rate_bps` | 2.0 (correct?) | **Bug**: should be ~0.35 bps for XMT | Needs fix |
| TXFD6 in symbol_map | mapped | **Bug**: TXFD6 missing, returns zero fees | Needs fix |

### Platform Bugs Found

1. `config/base/fees/futures.yaml` TX `tax_rate_bps: 2.0` may be incorrect (regulatory rate vs actual charged rate needs verification)
2. TXFD6 not in `symbol_map` — `FeeCalculator` returns zero fees for TXFD6
3. FeatureEngine v2 referenced in memory but not implemented in codebase (only v1 with 16 features exists)

---

## Root Cause Analysis

### Why microstructure alpha fails on TMFD6 at retail

1. **Cost floor too high**: 4.0 pts RT cost requires 65-107% accuracy depending on horizon. No L1 signal achieves >52%.

2. **Signal-horizon mismatch**: Signals work at 5-15s (IC=0.04) but costs require 60s+ horizons where signals decay to noise.

3. **L1 data too crude**: Median bid/ask qty = 3 contracts. Signal entropy is too low for meaningful prediction. The literature (Albers, Singha) uses trade-by-trade data with 15+ features.

4. **Adverse selection at wide spreads**: Wide spreads indicate toxicity, not opportunity. Getting filled during wide spreads = being adversely selected.

5. **No maker rebates**: The literature's profitable maker strategies (Albers on Binance) rely on -2.5 bps maker rebates. We pay to trade.

---

## Artifacts Produced

| File | Contents |
|------|----------|
| `docs/alpha-research/round16_stage1_survey.md` | Survey V1: 60+ papers, 3 candidates |
| `docs/alpha-research/round16_stage1_survey_v2.md` | Survey V2: expanded scope, 3 new candidates |
| `docs/alpha-research/round16_mc_validation_results.md` | MC-1~4 feasibility validations |
| `docs/alpha-research/round16_mc5_results.md` | Signal persistence at extended horizons |
| `docs/alpha-research/round16_mc6_results.md` | Execution optimization analysis |
| `docs/alpha-research/round16_mc789_tmfd6_results.md` | TMFD6 reversal, spread-conditional, exec opt |
| `docs/alpha-research/round16_push_response_results.md` | Push-response conditional mean-reversion |
| `docs/alpha-research/round16_survey2_updated_constraints.md` | Survey V2 alternate version |
| `outputs/team_artifacts/alpha-research/round16_*` | Challenger and Execution review artifacts |

---

## Lessons Learned

1. **Always verify cost model first.** Round 16 wasted cycles on stock-cost assumptions (3.5 bps) before discovering futures cost is 1.33 bps (XMT) or ~0.9 bps (TX). Cost model should be the FIRST gate, not discovered mid-analysis.

2. **L1 snapshot data is insufficient for microstructure alpha.** Trade-by-trade data (individual trades with timestamps, sizes, and side classification) is required for signals like entropy, Albers features, and VPIN. L1 bid/ask snapshots are too lossy.

3. **Wide spread ≠ opportunity.** Intuition says "wide spread = free money for makers." Data says wide spread = adverse selection trap. This is one of the most important negative results.

4. **Spread regime is structural, not predictable.** The Jan/Feb vs March spread difference is a contract maturity or market condition effect, not an intraday cycle. All signals that "work in wide-spread" are conditional on an unpredictable regime.

5. **Push-response shows regime-dependent behavior.** Negative pushes revert in wide-spread regimes but show momentum in tight-spread regimes. This contradicts the SPY paper's universal finding and suggests TMFD6 microstructure is qualitatively different.

---

## Future Directions (for Round 17+)

1. **TXO options data (33M rows)** — untapped, fundamentally different signal source
2. **Trade-level data pipeline** — if Shioaji provides individual trade records, re-attempt Albers/entropy
3. **Spread regime monitoring** — alert when wide-spread regime returns, activate push-response
4. **TX (大台) instead of XMT** — 2x lower cost in points, may make 10-15s signals viable
5. **Longer-term signals** — move beyond microstructure to intraday patterns, session effects, or cross-day signals
