# R18 Stage 2a: TMFD6 Adverse Selection Analysis (BLOCKER-E1)

**Date**: 2026-03-26
**Dataset**: `TMFD6_all_l1.npy` — 7,747,814 L1 rows, 43 sessions (~20 trading days)
**Method**: Mid-price change proxy (see limitations below)

## Verdict: PASS

Adverse selection rate at +5s is **43.7%** (< 60% kill threshold). Rate **decreases** with spread width — wider spreads offer MORE edge, not less. Spread-vol correlation is weak (+0.051) — wide spread is NOT predominantly information-driven.

---

## Methodology

### Trade Direction Inference

Volume field is always 0 in this L1 quote dataset, so trades are inferred from mid-price changes:
- Mid-price increase = buy-initiated (buyer lifted the ask)
- Mid-price decrease = sell-initiated (seller hit the bid)

**Limitation**: Only price-moving events are captured. Non-price-moving trades (passive fills within spread) are invisible. This biases the sample toward **informed flow**, making our adverse selection rates an **upper bound** on the true rate.

### Adverse Selection Definition

From the market maker's perspective:
- **Buy-initiated trade**: MM sold at ask. Adverse if mid goes UP further (informed buyer was right).
- **Sell-initiated trade**: MM bought at bid. Adverse if mid goes DOWN further (informed seller was right).

### Session Handling

TAIFEX day session (08:45-13:45 local) and night session (15:00-05:00 local) both included. Forward lookups do not cross session boundaries (detected via 30-min gaps). 43 sessions identified.

---

## Key Results

### Overall Adverse Selection Rates (spread >= 5 pts)

| Horizon | N valid | Adverse % | Avg Adverse Mag | Avg All Mag |
|---------|--------:|----------:|----------------:|------------:|
| +1s     | 176,158 | **34.4%** | 3.22 pts        | 2.15 pts    |
| +5s     | 176,034 | **43.7%** | 6.00 pts        | 4.89 pts    |
| +30s    | 175,403 | **49.3%** | 13.89 pts       | 12.92 pts   |

Adverse rate is below 50% at all horizons. At +1s (most relevant for fast MM exit), only 34.4% of fills face adverse movement.

### By Spread Bucket (+5s horizon)

| Spread (pts) | N trades | Adverse % | Avg Adv Mag |
|-------------|--------:|----------:|------------:|
| [5-9]       | 115,090 | **49.2%** | 5.59 pts    |
| [10-19]     |  28,396 | **43.3%** | 6.59 pts    |
| [20-39]     |  16,966 | **25.3%** | 7.04 pts    |
| [40+]       |  15,582 | **23.9%** | 8.95 pts    |

**Critical finding**: Adverse rate **monotonically decreases** from 49.2% at [5-9] to 23.9% at [40+]. Wider spreads = less adverse selection. This is the opposite of the "wide spread = informed trading" hypothesis. At spread >= 20pts, fewer than 1 in 4 fills face adverse movement at +5s.

### By Time of Day (+5s horizon)

| Session | N trades | Adverse % | Avg Adv Mag |
|---------|--------:|----------:|------------:|
| Opening 08:45-09:15 | 18,410 | **45.2%** | 7.89 pts |
| Midday 09:15-12:00  | 40,567 | **44.3%** | 5.19 pts |
| Closing 12:00-13:45 | 13,233 | **41.0%** | 4.05 pts |
| Night 15:00-21:00   | 33,856 | **41.9%** | 10.50 pts |
| Night 21:00-05:00   | 69,336 | **44.7%** | 4.17 pts |

Closing session has the lowest adverse rate (41.0%) and smallest magnitude (4.05 pts). Opening has the highest magnitude (7.89 pts) despite moderate rate — consistent with information arrival at open.

### MM Profit Potential (Favorable Fill Analysis)

Assuming MM captures half-spread and faces adverse/favorable mid-price movement:

| Horizon | Avg Half-Spread | Avg Movement | Gross P&L/fill | Net P&L/fill | % Profitable |
|---------|---------------:|-------------:|---------------:|-------------:|-------------:|
| +1s     | +7.42 pts      | -0.07 pts    | +7.35 pts      | **+5.35 pts**| **79.7%**    |
| +5s     | +7.43 pts      | -0.35 pts    | +7.07 pts      | **+5.07 pts**| **69.6%**    |
| +30s    | +7.44 pts      | -0.79 pts    | +6.64 pts      | **+4.64 pts**| **61.4%**    |

Half-spread capture (avg 7.4 pts) far exceeds adverse movement at all horizons. After 2pt one-leg cost, net P&L remains strongly positive: +5.07 pts/fill at +5s with 69.6% of fills profitable.

### Spread-Volatility Correlation

| Metric | Value |
|--------|------:|
| Pearson correlation | **+0.051** |
| [5-9] pts mean 1m vol | 0.000593 |
| [10-19] pts mean 1m vol | 0.000563 |
| [20-39] pts mean 1m vol | 0.000443 |
| [40+] pts mean 1m vol | 0.000607 |

Correlation is near-zero (+0.051). Wide spreads are NOT systematically followed by higher volatility — they appear to be **liquidity-driven** (thin book) rather than information-driven.

---

## Kill Gate Assessment

| Check | Threshold | Measured | Result |
|-------|-----------|----------|--------|
| Overall adverse rate +5s | > 60% = KILL | 43.7% | **PASS** |
| Rate increases with spread | Monotonic increase = WARNING | Decreasing | **FAVORABLE** |
| Spread-vol correlation | > 0.3 = WARNING | 0.051 | **PASS** |

---

## Implications for CBS / OpMM Strategy

1. **Wide spread is exploitable**: At spread >= 20pts, adverse rate is only 25% at +5s. The half-spread capture (10+ pts) vastly exceeds adverse movement.

2. **Night session viable**: 59% of wide-spread trade proxies occur in night session (103K/176K). Adverse rates are comparable to day session — night session is not systematically more informed.

3. **Closing session is safest**: 41.0% adverse rate with smallest magnitude. Good for conservative strategy variants.

4. **Average net P&L of +5 pts/fill is robust**: Even with upper-bound bias in our adverse selection measurement, the half-spread advantage dominates.

5. **Caveat**: These are trade proxies (mid-price changes), not actual fills. Real fill probability and queue priority effects are measured separately in BLOCKER-E2.

---

## Script Location

`research/experiments/validations/tmfd6_adverse_selection/measure_adverse_selection.py`
