# Diagnostic 0b: ExecutionOptimizer Baseline Analysis

**Date**: 2026-03-29
**Heuristic params**: spread_threshold_pts=2, fill_score_threshold=1.5, timeout=3s

## Methodology

Simulates the current ExecutionOptimizer heuristic against L1 research data.
A synthetic BUY trade is generated every ~500 ticks (~60s). For each trade:
- Heuristic decides LIMIT or MARKET based on spread, depth, imbalance.
- Limit orders are placed at best bid; fill simulated if ask crosses within 3s timeout.
- Cost: MARKET = half spread. LIMIT filled = 0. LIMIT timeout = half spread at timeout.

**Limitation**: No actual order flow or queue position modeling. This is a
best-case fill simulation (assumes front-of-queue). Real fills would be worse.

## TMFD6

| Date | Ticks | Decisions | LIMIT% | Fill Rate | Mkt Cost | Heur Cost | Savings | Optimal |
|------|-------|-----------|--------|-----------|---------|-----------|---------|---------|
| 2026-01-26 | 59,261 | 118 | 18.6% | 0.0% | 14.43 | 14.46 | -0.2% | 14.43 |
| 2026-01-27 | 223,016 | 446 | 21.7% | 0.0% | 15.99 | 15.89 | +0.6% | 15.99 |
| 2026-01-28 | 335,723 | 671 | 24.7% | 0.6% | 18.27 | 18.31 | -0.2% | 18.22 |
| 2026-01-29 | 450,922 | 901 | 23.5% | 0.0% | 21.74 | 21.81 | -0.3% | 21.74 |
| 2026-01-30 | 586,182 | 1172 | 21.1% | 0.8% | 22.52 | 22.59 | -0.3% | 22.50 |
| 2026-02-03 | 372,463 | 744 | 25.4% | 0.0% | 23.19 | 23.24 | -0.2% | 23.19 |
| 2026-02-04 | 585,904 | 1171 | 21.7% | 0.0% | 25.36 | 25.50 | -0.6% | 25.36 |
| 2026-02-05 | 213,476 | 426 | 20.9% | 0.0% | 22.77 | 22.65 | +0.5% | 22.77 |
| 2026-02-06 | 481,528 | 963 | 20.7% | 0.0% | 19.26 | 19.32 | -0.3% | 19.26 |
| 2026-02-10 | 69,311 | 138 | 28.3% | 0.0% | 17.92 | 18.00 | -0.4% | 17.92 |
| 2026-02-11 | 9,576 | 19 | 42.1% | 0.0% | 10.47 | 10.47 | +0.0% | 10.47 |
| 2026-02-23 | 475,814 | 951 | 22.3% | 2.8% | 5.18 | 5.15 | +0.6% | 5.17 |
| 2026-02-24 | 430,956 | 861 | 32.8% | 3.5% | 3.57 | 3.51 | +1.5% | 3.54 |
| 2026-02-25 | 200,250 | 400 | 30.5% | 4.9% | 3.23 | 3.16 | +2.2% | 3.19 |
| 2026-03-19 | 516,982 | 1033 | 30.8% | 52.5% | 1.40 | 1.16 | +16.8% | 1.17 |
| 2026-03-20 | 783,520 | 1567 | 31.7% | 51.2% | 1.46 | 1.20 | +17.5% | 1.23 |
| 2026-03-23 | 824,541 | 1649 | 31.4% | 56.8% | 1.95 | 1.56 | +19.7% | 1.60 |
| 2026-03-24 | 610,036 | 1220 | 30.8% | 58.5% | 1.60 | 1.32 | +17.6% | 1.32 |
| 2026-03-25 | 444,684 | 889 | 30.0% | 44.6% | 1.50 | 1.28 | +14.2% | 1.30 |
| 2026-03-26 | 73,669 | 147 | 33.3% | 53.1% | 1.45 | 1.16 | +20.0% | 1.19 |

**TMFD6 Summary** (20 days, 15486 decisions):
- Limit order rate: 26.9%
- Limit fill rate: 26.5%
- Avg cost (always-market): 10.63 pts/trade
- Avg cost (heuristic): 10.53 pts/trade
- **Cost savings**: +1.0%
- Avg cost (retrospective optimal): 10.51 pts/trade
- **Improvement ceiling** (heuristic vs optimal): 0.02 pts/trade

- Fill time (when filled): P50=875ms, P95=2631ms, mean=1075ms
- Spread when LIMIT chosen: mean=15.72, median=5.00
- Spread when MARKET chosen: mean=23.30, median=8.00

## TXFD6

| Date | Ticks | Decisions | LIMIT% | Fill Rate | Mkt Cost | Heur Cost | Savings | Optimal |
|------|-------|-----------|--------|-----------|---------|-----------|---------|---------|
| 2026-01-26 | 69,570 | 139 | 0.0% | 0.0% | 182.33 | 182.33 | +0.0% | 182.33 |
| 2026-01-27 | 151,120 | 302 | 2.6% | 0.0% | 136.29 | 136.28 | +0.0% | 136.29 |
| 2026-01-28 | 283,395 | 566 | 11.7% | 0.0% | 475.91 | 476.03 | -0.0% | 475.91 |
| 2026-01-29 | 338,452 | 676 | 33.4% | 0.0% | 194.88 | 194.40 | +0.2% | 194.88 |
| 2026-01-30 | 506,950 | 1013 | 52.9% | 0.0% | 230.41 | 230.29 | +0.0% | 230.41 |
| 2026-02-03 | 314,835 | 629 | 16.5% | 0.0% | 202.79 | 203.19 | -0.2% | 202.79 |
| 2026-02-04 | 501,126 | 1002 | 13.8% | 0.0% | 137.28 | 137.45 | -0.1% | 137.28 |
| 2026-02-05 | 172,116 | 344 | 32.6% | 0.0% | 96.14 | 96.17 | -0.0% | 96.14 |
| 2026-02-06 | 424,198 | 848 | 13.9% | 0.0% | 132.82 | 132.90 | -0.1% | 132.82 |
| 2026-03-19 | 342,657 | 685 | 39.4% | 38.5% | 1.96 | 1.67 | +14.7% | 1.67 |
| 2026-03-20 | 509,728 | 1019 | 36.4% | 35.6% | 2.07 | 1.82 | +12.1% | 1.83 |
| 2026-03-23 | 518,636 | 1037 | 35.8% | 36.4% | 2.99 | 2.52 | +15.7% | 2.67 |
| 2026-03-24 | 408,236 | 816 | 38.6% | 41.0% | 2.16 | 1.84 | +15.0% | 1.84 |

**TXFD6 Summary** (13 days, 9076 decisions):
- Limit order rate: 29.0%
- Limit fill rate: 19.0%
- Avg cost (always-market): 123.42 pts/trade
- Avg cost (heuristic): 123.30 pts/trade
- **Cost savings**: +0.1%
- Avg cost (retrospective optimal): 123.30 pts/trade
- **Improvement ceiling** (heuristic vs optimal): -0.01 pts/trade

- Fill time (when filled): P50=1120ms, P95=2642ms, mean=1198ms
- Spread when LIMIT chosen: mean=210.21, median=15.00
- Spread when MARKET chosen: mean=261.82, median=204.00

## Direction A Feasibility Assessment

The improvement ceiling (heuristic cost - retrospective optimal cost) represents
the maximum possible gain from a perfect fill probability model. If the ceiling
is < 0.3 pts/trade, Direction A is not worth the complexity.
