# R22 Stage 4 Backtest Report — 2026-03-28

## CBS + vrr Gating Backtest

### Configuration
- CBS-40-300: 40 bps / 600s detection → contrarian → 300s hold → 15 bps stop
- Cost: 3.92 pts RT | Latency: 36ms P95 | Data: TMFD6 L1 (20 days)
- IS: 14 days (Jan-Feb) | OOS: 6 days (March)
- Threshold sweep: [0.3, 0.5, 0.7, 1.0, 1.5, 2.0]

### OOS Results

| Threshold | Trades | Mean P&L (pts) | WR% | MaxDD | Filtered% | p-value |
|-----------|--------|----------------|------|-------|-----------|---------|
| BASELINE  | 285    | -8.12          | 36.1 | 2346  | 0%        | —       |
| vrr<0.3   | 110    | -8.52          | 39.1 | 1355  | 61%       | 0.523   |
| vrr<0.5   | 211    | -8.30          | 36.5 | 2142  | 26%       | 0.513   |
| vrr<0.7   | 244    | -8.00          | 35.2 | 2608  | 14%       | 0.492   |
| vrr<1.0   | 264    | -7.86          | 37.9 | 2634  | 7%        | 0.481   |
| vrr<1.5   | 272    | -9.14          | 36.0 | 3016  | 5%        | 0.574   |
| vrr<2.0   | 277    | -8.89          | 35.4 | 2954  | 3%        | 0.557   |

**Kill criterion: TRIGGERED.** No threshold achieves p < 0.10. Best p = 0.481 (vrr<1.0).

### Reviewer Verdicts

**Challenger**: KILL confirmed for CBS filter. CBS baseline is -8.12 pts/trade (structurally unprofitable at March spread=3 pts < 3.92 pts cost). Non-monotonic P&L pattern across thresholds = noise. MaxDD reduction is mechanical (fewer trades), not signal quality. **vrr feature APPROVE for commit as FE [21] with no strategy coupling.**

**Execution**: Methodology concerns noted but negative result accepted:
- (a) Alpha constant 0.87% drift between backtest and production — minor
- (b) Return computation mismatch (absolute vs fractional) — NOTE: production was FIXED in Stage 3 to use raw difference, matching backtest
- (c) Missing CBS session gate (09:15-13:35) — backtest includes opening trades production would skip
- (d) Warmup asymmetry — baseline can trade during vrr warmup period

Execution recommendation: Accept negative result. No CBS code changes.

### Conclusion

**vrr as CBS filter: KILLED** — cannot improve negative-expectation CBS on TMFD6 March cost regime.

**vrr as FeatureEngine feature: APPROVED FOR COMMIT** — both reviewers agree:
- Detrended IC +0.031 at 300s (not trend contaminated)
- Monotonic absolute-return prediction (55% Q1→Q5)
- Orthogonal (rho=0.053)
- O(1), trivial cost
- No strategy coupling — infrastructure feature only

## R22 Final Outcome

| Candidate | Stage 1 | Stage 2 | Stage 3 | Stage 4 | Final |
|-----------|---------|---------|---------|---------|-------|
| rv_ratio_regime | GO | GO | IMPLEMENTED | CBS FAIL | **Feature [21] only** |
| imbalance_mr_speed | GO | KILLED | — | — | **DEAD** |
| ofi_run_length | KILLED | — | — | — | **DEAD** |

**Net**: 1 FeatureEngine feature (`vrr_5_300_x1000` [21]), 0 strategy signals.
