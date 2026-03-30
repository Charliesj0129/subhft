# R22 Stage 1 Survey Report — 2026-03-28

## Candidates Evaluated

### 1. `rv_ratio_regime` — Multi-Scale Realized Volatility Ratio as CBS Filter
- **Papers**: Christensen/Turner/Godsill (2006.08307v1), Safari/Schmidhuber (2501.16772v2), Garcin (2105.09140v3)
- **Signal**: vrr = RV_5s / RV_300s (EW variance accumulators)
- **Target**: CBS regime gate (low vrr = calm = favorable for CBS entry)
- **Horizon**: 30-600s | **Computation**: O(1) per tick | **Data**: mid_price_x2 only
- **Feature index**: [22] `rv_ratio_x1000`
- **Challenger verdict**: CONDITIONAL APPROVE
  - Challenge 1: R20 "GO for 4h diagnostic" was NEVER executed — zero empirical evidence on TAIFEX
  - Challenge 2: Discrete mid_price makes RV_5s degenerate (~10-20% non-zero returns in 5s window)
  - Conditions: Run R20 diagnostic first; verify RV_5s non-degeneracy (>30% non-zero returns); test as CBS P&L conditioner with OOS on March data; kill if p >= 0.10
- **Execution verdict**: APPROVE (no conditions)
- **Consensus**: GO — run the R20 diagnostic that was promised but never delivered

### 2. `imbalance_mr_speed` — Imbalance Mean-Reversion Speed as Regime Detector
- **Papers**: Ruan/Bacry/Muzy (2303.02038v2), Brouty/Garcin/Roccaro (2407.17401v3), Safari/Schmidhuber (2501.16772v2)
- **Signal**: Online OU fit to imbalance → mr_speed = -ln(beta)/dt
- **Target**: CBS regime gate (high mr_speed = fast reversion = favorable for CBS)
- **Horizon**: 60-600s | **Computation**: O(1) per tick | **Data**: imbalance, mid_price_x2, spread_scaled
- **Feature index**: [21] `mr_speed_x1000`
- **Challenger verdict**: CONDITIONAL APPROVE
  - Challenge 1: TMFD6 imbalance may be near-binary (thin book, 3 contracts/level) — OU fit degenerate
  - Challenge 2: EMA accumulators risk trend contamination (R18 lesson)
  - Conditions: Gate Zero verify imbalance CV > 0.3 across 5s windows; detrended IC at all horizons; demonstrate incremental value over ret_autocov_5s [17]
- **Execution verdict**: APPROVE (no conditions)
- **Consensus**: CONDITIONAL GO — Gate Zero must verify non-degenerate imbalance first

### 3. `ofi_run_length` — OFI Run-Length Persistence Asymmetry (KILLED)
- **Papers**: Tsaknaki/Lillo/Mazzarisi (2307.02375v2), Sato/Kanazawa (2502.17906v4), Gontis (2006.00596v2)
- **Signal**: persistence_asymmetry from buy/sell run-length EMAs
- **Target**: Directional signal at 30-120s
- **Challenger verdict**: REJECT (2 unresolved challenges)
  - Run lengths too short on TMFD6 (125ms ticks vs NASDAQ 1ms)
  - L1 imbalance as OFI proxy known-lossy (R16 confirmed)
  - Effectively OFI variant — same family killed in R11, R16, R18, R19
- **Execution verdict**: APPROVE (conditional on detrended IC > 0.04)
- **Consensus**: KILLED — Challenger REJECT is final per team rules

## Recommendation

Proceed to Stage 2 with **2 candidates** in priority order:
1. **`rv_ratio_regime`** — lowest risk, R20 prior GO, orthogonal (rho=0.053), simplest computation
2. **`imbalance_mr_speed`** — highest novelty, but Gate Zero required to verify feasibility on TMFD6

Both are CBS regime gates (no independent trading), which avoids the cost-barrier problem that killed all standalone L1 signals in R16-R21.

## Awaiting User Confirmation

Select which candidates to advance to Stage 2 (prototype + Gate Zero diagnostic).
