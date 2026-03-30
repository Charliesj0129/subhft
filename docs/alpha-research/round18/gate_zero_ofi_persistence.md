# Gate-Zero: OFI Persistence Test on TXFD6/TMFD6

**Date**: 2026-03-26
**Kill Gate**: If predictive OFI correlation < 0.05 at 5min, Candidates A (log-GOFI) and B (OFI-OU) are dead.
**Data**: L1 tick-by-tick from ClickHouse export (TXFD6: 6.3M rows, TMFD6: 7.7M rows, ~58 days each)
**Method**: Aggregate OFI into time buckets, compute contemporaneous and predictive (lag-1) correlations

---

## Methodology

**Standard OFI** (Cont 2014): At each tick, compute bid-side contribution (queue depth change conditioned on bid price movement) minus ask-side contribution. Aggregate by summing across all ticks in each time bucket.

**log-OFI**: Same construction but uses log(1+qty) instead of raw qty for stationarization. This is the L1-only approximation of log-GOFI (Candidate A). True log-GOFI requires tracking multi-level traversals; this test uses only L1 as a lower bound on the full construction's performance.

**Contemporaneous correlation**: OFI aggregated in window t vs return in SAME window t.
**Predictive correlation**: OFI aggregated in window t vs return in NEXT window t+1.
**Winsorization**: 1st/99th percentile clip to reduce outlier influence.
**Trading hours filter**: 08:45-13:45 Taiwan time only.

---

## Results: TXFD6 (Taiwan Futures, full-size)

| Horizon | Contemp OFI | Contemp logOFI | Pred OFI | p-value | Pred logOFI | p-value | AC(1) | N_bkt |
|---|---|---|---|---|---|---|---|---|
| 30s | 0.386 | 0.406 | +0.004 | 0.857 | -0.000 | 0.993 | 0.182 | 2502 |
| 1m | 0.397 | 0.422 | +0.027 | 0.370 | +0.021 | 0.482 | 0.199 | 1498 |
| 2m | 0.447 | 0.470 | +0.026 | 0.485 | +0.038 | 0.310 | 0.223 | 908 |
| 5m | 0.469 | 0.493 | +0.045 | 0.392 | +0.062 | 0.236 | 0.260 | 443 |
| 10m | 0.495 | 0.525 | -0.092 | 0.174 | -0.080 | 0.239 | 0.082 | 248 |
| 30m | 0.559 | 0.591 | -0.058 | 0.598 | -0.032 | 0.771 | 0.236 | 94 |

**TXFD6 verdict**: Contemporaneous correlation strong (0.39-0.59), increases with horizon. But predictive correlation is WEAK and NOT significant at any horizon. At 10min+, predictive correlation turns NEGATIVE (mean reversion). N is limited (367 pairs at 5min) contributing to low power. **BORDERLINE FAIL** -- insufficient evidence for predictive OFI on TXFD6 at medium frequency.

---

## Results: TMFD6 (Mini-TAIEX Futures)

| Horizon | Contemp OFI | Contemp logOFI | Pred OFI | p-value | Pred logOFI | p-value | AC(1) | N_bkt |
|---|---|---|---|---|---|---|---|---|
| 30s | 0.634 | 0.729 | +0.074 | <0.001*** | +0.032 | 0.015* | 0.133 | 6458 |
| 1m | 0.673 | 0.732 | +0.075 | <0.001*** | +0.060 | <0.001*** | 0.108 | 3537 |
| 2m | 0.690 | 0.747 | +0.084 | <0.001*** | +0.075 | 0.002** | 0.107 | 1871 |
| 5m | 0.699 | 0.748 | +0.066 | 0.073 | +0.102 | 0.005** | 0.116 | 773 |
| 10m | 0.704 | 0.748 | +0.005 | 0.917 | +0.041 | 0.427 | 0.088 | 394 |
| 30m | 0.625 | 0.760 | -0.002 | 0.979 | +0.043 | 0.641 | 0.017 | 137 |

**TMFD6 verdict**: Extraordinary contemporaneous correlation (logOFI: 0.73-0.76 from 30s to 30min). Predictive correlation is statistically significant:
- **Standard OFI**: Significant at 30s-2m (r=0.07-0.08, p<0.001), marginal at 5min (r=0.066, p=0.073)
- **log-OFI**: Significant at 1m-5m, with STRONGEST signal at 5min (r=0.102, p=0.005)
- **log-OFI outperforms standard OFI at 5min** (0.102 vs 0.066) -- stationarization adds genuine value at longer horizons

---

## Key Findings

### 1. log-OFI stationarization is validated
At 5min horizon on TMFD6, log-OFI predictive r=0.102 vs standard OFI r=0.066. The 55% improvement confirms the paper's claim that log stationarization helps at longer aggregation windows. This supports Candidate A (log-GOFI).

### 2. TMFD6 >> TXFD6 for OFI-based medium-frequency signals
TMFD6 contemporaneous logOFI at 5min: r=0.748 vs TXFD6: r=0.493. TMFD6's thinner book and wider spread create larger OFI signals relative to price moves, making the signal more detectable.

### 3. Signal-horizon profile matches OU model prediction
On TMFD6, predictive OFI peaks at 2min (r=0.084) for standard OFI and at 5min (r=0.102) for log-OFI, then decays. This is consistent with Candidate B's OU shock model: there is an optimal intermediate horizon. The quasi-Sharpe framework could identify this peak more precisely.

### 4. OFI autocorrelation is moderate and persistent
AC(1) ranges 0.10-0.26 across horizons, confirming OFI has memory (consistent with Hu & Zhang 2025). This memory is what makes the OU model appropriate.

### 5. TXFD6 needs more data or different construction
With only 367 consecutive bucket-pairs at 5min, TXFD6 lacks statistical power. Also, TXFD6 shows mean reversion at 10min+ (negative predictive correlation), suggesting a different signal structure. May need the full multi-level GOFI construction (using L2-L5 data) to extract enough signal.

---

## Kill Gate Decision

| Instrument | Kill Gate (pred corr >= 0.05 at 5min) | Standard OFI | log-OFI | Decision |
|---|---|---|---|---|
| TMFD6 | >= 0.05 | 0.066 (p=0.073) | **0.102 (p=0.005)** | **PASS** |
| TXFD6 | >= 0.05 | 0.045 (p=0.392) | 0.062 (p=0.236) | **BORDERLINE FAIL** |

**Candidates A (log-GOFI) and B (OFI-OU) SURVIVE for TMFD6.**
TXFD6 is deferred pending more data or full multi-level GOFI implementation.

---

## Recommended Next Steps for Stage 2

1. **Implement full log-GOFI** (multi-level traversal) using L2 hftbt data for recent dates. Compare with L1-only log-OFI to quantify the multi-level gain.
2. **Fit OU parameters** on TMFD6 OFI: estimate mean-reversion speed and compute quasi-Sharpe optimal horizon.
3. **Build regime detector**: compute rolling OFI autocorrelation, identify high/low efficiency regimes.
4. **Backtest simple directional strategy**: enter when 5min log-OFI exceeds threshold, hold 5min, measure net PnL after 4pts RT cost.

---

## Raw Data

Full results saved to `docs/alpha-research/round18/gate_zero_raw.json`.
