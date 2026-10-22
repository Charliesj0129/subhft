---
name: taifex-market-structure
description: "TAIFEX facts for this platform: point values, cost derivation, sessions, spread regimes, price scales, data conventions. Use when making cost, spread, fill, or sizing assumptions or reading TAIFEX data. Not for alpha kill criteria (taifex-alpha-kill-criteria)."
---

# TAIFEX market structure

Numbers that drift (fees, spreads, margins) are derived or read from their
source, not copied. Where this file and a config disagree, find out which is
right before using either.

## Instruments and point values

| Product | Code family | 1 point = | Tick |
|---|---|---|---|
| Micro TAIEX futures (微台指) | TMF | 10 NTD | 1 pt |
| Mini TAIEX futures (小台) | MXF / MTX | 50 NTD | 1 pt |
| TAIEX futures (大台) | TXF / TX | 200 NTD | 1 pt |
| TAIEX options | TXO | premium-based; strike interval 50 pts near ATM, 100 OTM | 0.1 pt min premium tier |

MXF is the mini, not the micro: confusing them caused a 5x risk error. Confirm
the product code before computing margin, stops, or PnL. Report futures PnL in
points or NTD, never bps; spread thresholds are in points.

## Costs (retail, no maker rebate, no institutional tier)

Derive per side, then round trip:

```
tax per side  = price x 0.002%      (index futures; buyer AND seller)  -> ~1 pt at 48,500
commission    = broker-specific range (NTD/side; TXF tens, MXF ~15-50, TMF ~8-20)
taker adds    = the spread crossed
round trip    = 2 x (tax + commission) [+ spread if taker]
```

Source of record and the correction history: memory `feedback_taifex_fee_structure`
(corrected 2026-10-06). `config/research/cost_profiles.yaml` holds the backtest
profiles and may lag this derivation; check its header before trusting it.
Options: premium-based tax with a statutory floor; retail option spreads are
usually the binding cost.

## Spread regime

Spreads are non-stationary: a signal that works at wide spreads can fail at
tight ones, and imbalance signals can reverse sign across regimes. Always check
the most recent month's spread distribution before assuming a regime, and test
signals conditional on spread. Most trades are single-lot, so volume weighting
and inventory skew add little.

## Sessions (Asia/Taipei, UTC+8)

Day 08:45-13:45; night 15:00-05:00 the next day. Reconnect windows are
`HFT_RECONNECT_HOURS` / `HFT_RECONNECT_HOURS_2`. The 05:00 close is also the
nightly UTC 21:00 boundary: schedules and the TAIFEX roll fall on it. Contract
months roll around the third Wednesday (`symbols-sync`).

## Data conventions

- Platform prices x10000; research ClickHouse raw and golden parquet x1,000,000
  (divide by 100 to reach platform scale); option strikes use another scale.
- ClickHouse stores up to five book levels, variable length 0-5; filter on
  `length(bids_price)` (`.agent/rules/70-research-data.md`).
- hftbacktest depth events are delta-incremental with qty=0 removals;
  level accumulation once collapsed a 4-point spread to 1 (fixed 2026-04-10;
  re-export older data).
- Local archive coverage and holes: `docs/operations/local-clickhouse-market-data-corpus.md`.

## Done when

Costs are derived with stated inputs, the product code and point value are
confirmed, the recent-month spread was checked, and the price scale of every
data source is stated.
