---
name: taifex-market-structure
description: Use when working with TAIFEX instruments (TMFD6, TXFD6, TXO), analyzing market data, or making any cost/spread/fill assumptions. Encodes Taiwan futures microstructure facts, fee schedules, and data conventions.
---

# TAIFEX Market Structure

Domain knowledge for Taiwan Futures Exchange instruments. All numbers are empirically validated from production data (2026 Q1-Q2).

## Instrument Economics

### TMFD6 (微型臺指期)

| Property | Value |
|----------|-------|
| Point value | **1 pt = 10 NTD** (NOT 50) |
| Tick size | 1 pt |
| Commission | ~13 NTD per side (retail) |
| Tax | ~7 NTD per side (sell only, 2.0 bps) |
| **RT cost** | **4.0 pts = 40 NTD** |
| Breakeven spread | > 4 pts (capture ≥ 5 pts to profit) |
| Margin (initial) | ~23,000 NTD |
| Typical daily range | 50-200 pts |

### TXFD6 (臺指期)

| Property | Value |
|----------|-------|
| Point value | **1 pt = 200 NTD** |
| Tick size | 1 pt |
| Commission | ~30 NTD per side (retail) |
| Tax | ~40 NTD per side (2.0 bps) |
| **RT cost** | **~0.48 pts = 96 NTD** |
| Breakeven spread | > 1 pt |
| Margin (initial) | ~184,000 NTD |

### TXO (臺指選擇權)

| Property | Value |
|----------|-------|
| Strike interval | 50 pts (near ATM), 100 pts (OTM) |
| Minimum premium | 0.1 pt = 5 NTD |
| RT spread cost | ~80 pts (4.68 pts effective) |
| **Retail spreads** | Structural blocker for most strategies |

### Fee Structure (Retail Individual Account)

```
No maker rebates — user is individual retail, not institutional.
No fee negotiation — standard TAIFEX retail schedule.

Per-side cost breakdown:
  Commission: exchange fee + broker fee (varies by broker)
  Tax: 2.0 bps on sell side only (futures)
  Total RT: commission_buy + commission_sell + tax_sell
```

## Spread Regime Facts

### TMFD6 Spread Distribution (Non-Stationary)

| Period | Median spread (pts) | Pct ≥ 4 pts | Trading character |
|--------|---------------------|-------------|-------------------|
| 2026 Jan-Feb | 28-68 | 70%+ | Wide spreads, MM profitable |
| 2026 Mar onwards | 3 | 33% | Tight spreads, MM breakeven risk |

**Critical insight**: IC ∝ spread width. Signals that work at wide spreads fail at tight spreads.

| Spread regime | QI_1 IC | Interpretation |
|---------------|---------|----------------|
| ≤ 5 pts (tight) | -0.046 | **Reversed** — imbalance is noise/spoofing |
| > 30 pts (wide) | +0.305 | Strong — imbalance is real information |

### Variance Ratio (Mean-Reversion Indicator)

```
VR(540) statistics:
  TMFD6: mean=0.86, trending (VR>1.2) only 18% of time
  TXFD6: mean=0.73, trending (VR>1.2) only 7% of time

Interpretation: Markets are predominantly mean-reverting at 9-min scale,
but trend-following alpha is structurally unmonetizable (edge < cost).
```

## Liquidity Patterns

### Trade Size Distribution

```
TMFD6: 73% of trades are qty=1
→ Inventory skew useless at this scale
→ Volume-weighted signals add no information over equal-weight
```

### Intraday Patterns

```
Last 30 min of session: 41% of MM PnL, highest spreads (avg 4.62 pts)
Early morning (08:45-09:30): Tightest spreads, highest adverse selection
Mid-session (10:00-12:00): Moderate, stable
```

### Session Hours (Asia/Taipei, UTC+8)

```
Day session:   08:45 — 13:45
Night session: 15:00 — 05:00 (next day)

HFT_RECONNECT_HOURS=08:30-13:35  (day)
HFT_RECONNECT_HOURS_2=14:55-05:05 (night)
```

## Data Conventions

### Price Scaling

```python
# Platform convention: ALL prices scaled x10000
# Example: TMFD6 quote at 22500 pts → stored as 225000000

# Golden parquet files (research): prices scaled x1,000,000 (NOT x10,000)
# Always check data source before assuming scale factor

price_platform = 225000000      # x10000
price_golden = 22500000000      # x1000000 (golden parquet)
```

### ClickHouse Data

```
CK stores L1-L5 order book levels
Export with: --formats l5 (not just L1)

Tables:
  hft.market_data — tick-by-tick trades
  hft.orders — order lifecycle
  hft.trades — fill records
```

### hftbacktest Data Format

```python
# HftBacktest V2 event format
# DEPTH_EVENT: level-based incremental updates
# TRADE_EVENT: execution reports
# Must use delta-incremental with qty=0 removals
# BUG (fixed 2026-04-10): level accumulation collapsed spread 4→1 pt
```

## Backtest Cost Parameters

```yaml
# Standard TAIFEX retail parameters for backtesting
maker_fee_bps: -0.2      # Effective maker fee (commission as bps)
taker_fee_bps: 0.2        # Effective taker fee
sell_tax_bps: 2.0          # Sell-side tax (futures)

# Latency profile (Shioaji sim)
submit_ack_latency_ms: 36   # P95
modify_ack_latency_ms: 43   # P95
cancel_ack_latency_ms: 47   # P95
```

## Key Metrics Conventions

### Spread Thresholds: POINTS not bps

```python
# CORRECT: threshold in points (invariant to price level)
spread_threshold_pts = 5   # TMFD6 breakeven = 4 pts + 1 pt margin

# WRONG: bps shifts with price level
spread_threshold_bps = 0.02  # DO NOT USE
```

### PnL Reporting

```
PnL always in points (pts) or NTD, never in bps for futures
  TMFD6: 1 pt = 10 NTD
  TXFD6: 1 pt = 200 NTD

Example: R47 +4,534 pts over 12 days = +45,340 NTD (TMFD6)
```

## Anti-Patterns

- Do NOT assume 1 pt = 50 NTD for Mini-TAIEX (it's 10 NTD)
- Do NOT use bps for spread thresholds (use points)
- Do NOT assume maker rebates exist (retail has none)
- Do NOT mix golden parquet scale (x1M) with platform scale (x10K)
- Do NOT assume spread stationarity — check recent-month regime first
- Do NOT use volume-weighted signals on TMFD6 (73% trades are qty=1)
