# Round 16 Stage 1: q-fin.TR Literature Survey

## Direction
Search q-fin.TR (Trading and Market Microstructure) papers on arXiv for profitable strategies.

## Search Coverage
~80 papers reviewed across 10 search angles (2024-2026).

## 3 Candidate Directions Proposed

### Candidate A: Contrarian Fill-Probability-Aware OpportunisticMM
- Papers: 2502.18625, 2307.04863, 2403.02572, 2508.16588, 2505.12465
- Enhance OpMM with: fill probability model + contrarian imbalance bias + expanded selective quoting
- Claimed improvement: +0.32 → +0.5-0.8 bps/RT

### Candidate B: TXO Variance Risk Premium Harvesting
- Papers: 2508.16598, 2504.06208
- Sell short-dated OTM TXO puts, delta-hedge with TXFD6
- Structural VRP edge, non-directional

### Candidate C: Latency-Aware Queue-Optimal Order Placement
- Papers: 2403.02572, 2512.05734, 2508.20225, 2505.12465
- Execution optimization layer, not new alpha

## Review Verdicts

| Candidate | Challenger | Execution | Consensus |
|-----------|-----------|-----------|-----------|
| A: Contrarian OpMM | NEEDS-DATA | APPROVE (caveats) | NEEDS-DATA |
| B: TXO VRP | REJECT | REJECT | REJECT |
| C: Queue-Optimal | APPROVE (conditional) | APPROVE | APPROVE (conditional) |

## Challenger Key Challenges

### Candidate A (3 challenges):
1. **Rebate-dependent economics**: Paper 2502.18625 (Binance) includes +1.0 bps/RT maker rebate. Strip rebate: raw capture = -0.29 bps/RT. Transfer to TAIFEX unsubstantiated.
2. **Missing trade-side classification**: Fill prob model needs buy/sell (not in TickEvent). 22 days (~1.3M ticks) grossly insufficient for ML.
3. **+0.32 bps baseline is L1 only**: Round 13 L2 queue-aware backtest -> Sharpe -100 to -125. Baseline may be artifact.

### Candidate B (3 challenges):
1. **TXO fee structure**: OTM put spread = 5-20% of premium. Delta-hedging ~NT$870/day. Need 40%+ annual VRP to break even.
2. **4-6 week timeline wildly optimistic**: 8 missing infra components -> 3-6 months realistic.
3. **No concrete PnL estimate**.

### Candidate C (2 challenges):
1. **Queue position unobservable**: TAIFEX doesn't provide individual queue position.
2. **ROI negative at current scale**: OpMM trades 2.1% of time -> uplift ~NT$6,300-12,600/year.

## Execution Key Concerns

### Candidate A:
1. Zero fill probability infrastructure (no queue tracking, no fill/miss dataset, no ML inference pipeline).
2. OpMM code refactor larger than stated - need asymmetric quoting, FeatureEngine wiring, ~1 extra week.
3. 250us pipeline budget sufficient for lookup table / heuristic, not ML model.

### Candidate B:
1. No greeks, pricer, IV surface, delta-hedge engine, or options risk module. 3-4 months minimum.
2. Options contract resolution + order placement works via Shioaji, but analytics stack absent.

### Candidate C:
1. ImbalanceTimer proves middleware pattern architecturally feasible.
2. OrderIntent needs optional `price_type` field for dynamic limit/market selection.
3. 100-150us budget available - sufficient for heuristic.

## Challenger Data Requests (for Candidate A to proceed)

1. Per-RT PnL decomposition on TAIFEX: raw spread capture, adverse selection, rebate (0), commission, tax, net.
2. List exact substitute features from TickEvent + BidAskEvent for trade-side features. Effective sample size for 22 days.
3. OpMM PnL under L2 queue-aware fill assumptions.

## Execution Recommendation
Prioritize C first (smallest scope, ImbalanceTimer precedent), then layer A on top.

## Date: 2026-03-25
