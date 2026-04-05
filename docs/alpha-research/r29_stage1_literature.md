# R29 Stage 1: Literature Exploration — 大戶方向 × K線 × 日內交易 (1min-3hr)

**Date**: 2026-04-02
**Researcher**: Opus
**Status**: Awaiting user selection (v2 — corrected to intraday holding)

## Key Literature Findings (v2 — Intraday Focus)

1. **Metaorder detection from public data** is now feasible (Maitrier et al. 2025, arXiv 2503.18199). Institutional order-splitting creates persistent autocorrelated flow over 5-30min windows. Square-root impact law, concave trajectory, post-execution 2/3 decay.

2. **Intraday trending regime exists at 1hr-3hr scale** (Safari & Schmidhuber 2025, arXiv 2501.16772). Markets are trending at "few hours" scale, mean-reverting at shorter and longer scales. HMM can classify regime in real-time (Christensen et al. 2020, arXiv 2006.08307).

3. **Volume anomaly = regime switch signal** (Zhang & Rosenbaum 2023, arXiv 2304.05115). Calm→active liquidity mode switches predict returns over following 30+ minutes.

4. **K-line patterns remain weak standalone** but opening range (first 30min) has moderate predictive power for rest-of-day direction (Gao et al. 2018, JFE).

## Three Candidate Directions (Intraday 1min-3hr)

### Candidate 1: Intraday Metaorder Momentum (IMM)
- **Hypothesis**: Detect institutional metaorder-splitting from volume-clock signed flow → ride the persistent impact trajectory
- **Mechanism**: Volume-time bucketed cumulative flow exceeds adaptive threshold → enter in flow direction → exit on flow reversal or decay
- **Holding**: 10min - 2hr
- **Data**: Tick data in ClickHouse (have), volume-clock resampling (new)
- **Platform fit**: Moderate — BurstDetector overlaps but needs volume-clock extension
- **Kill risk**: TX may lack sufficient metaorder activity for detection
- **Key papers**: Maitrier et al. (2503.18199), Sato & Kanazawa (2301.13505), Mu et al. (1003.0168)

### Candidate 2: Biased Intraday Trend Following (BITF)
- **Hypothesis**: 三大法人 daily net position as pre-session directional bias + HMM regime filter → trend-follow only when regime=trending AND direction matches institutional bias
- **Mechanism**: Pre-session bias from T-1 institutional data → 30min opening observation → HMM trend/reversion classifier on 5min bars → trade in aligned direction → exit on regime switch
- **Holding**: 30min - 3hr
- **Data**: 1-min parquet (have) + 三大法人 daily data (public, needs scraper)
- **Platform fit**: Good — leverages RegimeClassifier (R24), EMA features
- **Kill risk**: 5hr session limits to 1-2 trades/day; daily bias is stale and widely known
- **Key papers**: Safari & Schmidhuber (2501.16772), Christensen et al. (2006.08307), Gao et al. (2018 JFE)

### Candidate 3: Volume-Regime Breakout (VRB)
- **Hypothesis**: Volume deviation from expected U-curve + directional flow at mode-switch → ride information diffusion trajectory
- **Mechanism**: Monitor actual vs expected intraday volume → detect calm→active transition → enter in signed flow direction → exit on return to calm
- **Holding**: 30min - 2hr
- **Data**: Tick + depth data in ClickHouse (have), rolling volume profile (computable)
- **Platform fit**: Best — BurstDetector was built for this exact detection
- **Kill risk**: Volume spikes may be noise (program trading, rebalancing) without directional persistence
- **Key papers**: Zhang & Rosenbaum (2304.05115), Lee & Park (2411.10956), Krause et al. (1812.07369)

## Comparative Matrix

| Dimension | IMM | BITF | VRB |
|---|---|---|---|
| Novelty vs R6-R28 | Medium-high | High | Medium |
| Literature strength | Strong | Moderate | Moderate |
| Data available? | Yes (tick) | Yes (1-min + scraper) | Yes (tick+depth) |
| Platform fit | Moderate | Good | Best |
| Holding period | 10min-2hr | 30min-3hr | 30min-2hr |
| Trades/day | 2-5 | 1-2 | 2-4 |
| Main kill risk | Insufficient metaorder activity | Stale daily bias, few trades | Noise volume spikes |

## User-Selected Architecture (v3)

**D1 (OFI continuation) + D3 (exhaustion exit) + D4 (regime filter) + D2 (absorption breakout independent)**

## Challenger Verdict (≥2 challenges per direction)

### D1 Challenges
- **C1.1**: OFI at L1 already exhausted in R11/R17/R22/R27. Cumulative OFI at 300s ≈ existing feature. Must show incremental IC > 0.02.
- **C1.2**: "Low resilience" = R22 VRR (never registered, no signal). OFI × resilience likely redundant (corr > 0.5?).
- **C1.3**: Needs regime filter to work = not standalone signal. Must pass Gate C alone first.

### D2 Challenges
- **C2.1**: 5-15 events/day × 120 days = 600-1800 samples. Insufficient for robust Gate C after train/test split.
- **C2.2**: Iceberg detection needs order-level data; L5 snapshots conflate icebergs with normal refill.
- **C2.3**: "10-30pt moves" may be base rate, not conditional excess. Must measure excess over unconditional.

### D3 Challenges
- **C3.1**: Mean reversion killed in R14/R17/R18. As exit signal = still a reversion bet at exit point.
- **C3.2**: Features exist since Phase 18, 8 rounds found no signal. Burden on novel combination logic.

### D4 Challenges
- **C4.1**: 120 days for HMM with 2-3 states = textbook overfit. Strict temporal CV required.
- **C4.2**: "Double Sharpe" claim unsubstantiated. R28 regime was 2-day artifact.

### Architecture Challenges
- Each component must pass Gate C independently before combination.
- D2 deferred until event frequency confirmed (≥500 in available data).
- D3 exit optimization is last step, after entry signals validated.

## Execution Verdict: CONDITIONAL PASS

### Feature Inventory
| Feature | Status | Effort | Used By |
|---------|--------|--------|---------|
| ofi_l1_cum/ema5s/ema30s | EXISTS | -- | D1,D3,D4 |
| deep_depth_momentum | EXISTS | -- | D1,D3 |
| tob_survival_ms | EXISTS | -- | D1,D2,D3,D4 |
| toxicity_ema50 | EXISTS | -- | D1,D2 |
| BurstDetector | EXISTS | -- | D1,D2 |
| RegimeClassifier | EXISTS (tick) | -- | D4 base |
| **MLOFI L1-L5** | **NEW** | medium | D1 |
| **Book resilience** | **NEW** | medium | D1 |
| **log-GOFI** | **NEW** | small | D1 |
| **Absorption detector** | **NEW** | large | D2 |
| **Session regime HMM** | **NEW** | medium | D4 |

### Cost/Risk Notes
- Config drift = 0, all additive
- Stop-loss must cap at 30pts (6000 NTD) to leave headroom under 8000 NTD hard limit
- D1+D2 concurrent lots must not exceed max_position_lots ceiling

### Recommended Execution Order
1. Phase 1: D3 (zero new features) + D4 simple rules prototype
2. Phase 2: D1 (MLOFI + resilience + log-GOFI) with D3 exit
3. Phase 3: D2 (absorption) only if D1 shows signal
4. Phase 4: D4 HMM upgrade if simple rules insufficient

## Key References
- Cont et al. (2010) 1011.6402 — OFI foundational
- Su et al. (2021) 2112.02947 — log-GOFI 83-86% R²
- Xu et al. (2019) 1907.06230 — MLOFI
- Patzelt & Bouchaud (2017) 1706.04163 — Extreme OFI pins price (absorption)
- Xu et al. (2016) 1602.00731 — LOB resilience post-aggressive-orders
- Ackermann et al. (2021) 2112.03789 — Positive vs negative resilience
- Christensen et al. (2020) 2006.08307 — HMM intraday momentum
- Safari & Schmidhuber (2025) 2501.16772 — Trending at hours scale
- Zotikov & Antonov (2019) 1909.09495 — CME iceberg detection
