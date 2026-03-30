# Round 16: TMFD6 OpMM Research — Final Report

**Date**: 2026-03-26
**Verdict**: NEGATIVE — OpMM not viable on TMFD6 in current market regime
**Scope**: q-fin.TR literature survey → FeatureEngine v2 → exhaustive parameter sweep → March validation

## Research Pipeline

### Stage 1: q-fin.TR Literature Survey
- 50+ papers surveyed, 3 deep-read (Albers 2502.18625, Zhang 2504.20349, Takahashi 2508.06788)
- Candidate A: Depth-normalized OFI (Takahashi) → implemented as feature
- Candidate B: Reversal-conditioned OpMM (Albers) → implemented as filter
- Candidate C: Fill probability model → rejected by Challenger (no queue position data)

### Stage 2: Python Prototype
- FeatureEngine v2: 3 new features (ofi_depth_norm_ppm, ret_autocov_5s_x1e6, tob_survival_ms)
- OpMM reversal filter: configurable 3-condition gate
- SimpleMM: inventory_skew_divisor parameterizable
- FeeCalculator: XMT flat-fee support
- 137 tests passing

### Stage 3: Data Extraction
- TMFD6 L1 data: 7.75M ticks, 20 trading days (2026-01-26 to 2026-03-26)
- March subset: 3.25M ticks, 6 days

### Stage 4: Backtest (3 iterations)

**v1 (naive)**: All thresholds negative. Fill model bug (ask-crosses-bid = spread inversion).

**v2 (production-parity-lite)**: Profitable at thr >= 10 pts. But Challenger/Execution FAILed:
- Fill discount was random, not adverse-selection-aware
- Config drift: quoting logic, threshold units, stop-loss, position limits
- 11-day sample, t-stat < 2.0

**v3 (production-parity)**: All thresholds negative. Inventory skew (5x larger) + stop-loss killed profitability.

**Sweep (1,080 configs)**: 77 profitable (7.1%). All winners: passive exit + no skew + no stop.
Best: thr=7bps, +10,410 pts (sweep) / +823 pts (validated, 12.6x overestimate from exit-fill model).

**March-only validation**: ALL configurations negative. No exceptions.

### Root Cause

| Period | Median Spread | >= 5 pts | Verdict |
|--------|--------------|----------|---------|
| Jan-Feb | 7 pts | 58% | Profitable (anomalous wide spread) |
| March | 3 pts | 5% | **Negative (spread < cost)** |

March median spread (3 pts) < RT cost (3.92 pts). The "45.5% profitable spread" was a period effect in Jan/Feb (possibly contract rollover, volatility spike, or low liquidity), not a structural property of TMFD6.

## Cost Model (Verified)

- Tax: 6.6 NTD/side (十萬分之二 × 330,000 NTD contract value)
- Commission: 13 NTD/side (user's broker)
- RT: 39.2 NTD = 3.92 pts = 1.19 bps

## Key Findings

1. **OpMM on TMFD6 is structurally unviable in normal (March) market conditions**
2. **Spread regime is non-stationary** — Jan/Feb anomaly inflated aggregate statistics
3. **Inventory skew kills 1-lot MM** — production formula too aggressive for single-lot
4. **Passive exit dominates** — 100% of profitable configs use passive fills, never cross spread
5. **Fill model sensitivity** — sweep vs validation showed 12.6x overestimate
6. **Always validate on most recent data** — aggregated stats over mixed regimes are misleading

## Platform Deliverables

Despite negative alpha result, durable infrastructure was built:

| Deliverable | Status | Files |
|---|---|---|
| FeatureEngine v2 (21 features) | Merged | `feature/engine.py`, `feature/registry.py` |
| OpMM reversal filter | Merged | `strategies/opportunistic_mm.py` |
| SimpleMM skew parameterization | Merged | `strategies/simple_mm.py` |
| XMT fee support | Merged | `tca/fee_calculator.py`, `config/base/fees/futures.yaml` |
| TMFD6 L1 data pipeline | Done | `research/data/raw/tmfd6/` (7.75M ticks) |
| Backtest suite (v1-v3 + sweep) | Done | `research/experiments/validations/tmfd6_opmm/` |

## Artifacts

- `docs/alpha-research/round16_stage1_qfin_tr_survey.md` — Literature survey
- `research/experiments/validations/tmfd6_opmm/` — All backtest scripts and results
- `research/data/raw/tmfd6/TMFD6_all_l1.npy` — 7.75M tick L1 data
- `research/data/raw/tmfd6/TMFD6_march_l1.npy` — March-only subset
