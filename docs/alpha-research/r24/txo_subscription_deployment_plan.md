# R24: TXO Subscription Deployment Plan

**Date**: 2026-03-30
**Purpose**: Enable TXO options data collection for Direction B (Cross-Instrument Options Flow)
**Status**: Ready for manual deployment

---

## Prerequisites

- [x] ClickHouse instrument columns exist (migration `20260330_001`)
- [x] InstrumentRegistry supports options (`InstrumentType.OPTION`, `OptionRight`, etc.)
- [x] FOP tick/bidask callbacks registered in `quote_runtime.py:244-247`
- [x] ContractsRuntime handles `product_type="option"` (contracts_runtime.py:117-125)
- [x] Normalizer `_populate_registry()` reads options fields from symbols.yaml
- [x] Recorder populates instrument metadata via InstrumentRegistry

## Changes Made

### 1. Symbol Config

**Base config** (`config/base/symbols.yaml` -- committed, version controlled):
Added 22 TXO symbols: ATM +/- 5 strikes (22500-23500, step 100), front-month April 2026 (D6/P6).

Each entry includes full metadata:
- `exchange: OPT`, `product_type: option`
- `underlying: TX`, `point_value: 50` (NTD/pt)
- `tick_size: 1.0` (correct for ATM premiums >> 10 pts)
- `right: C/P`, `strike: <value>`, `expiry: "2026-04-15"`
- `tax_rate_bps: 10`, `commission_per_lot: 150000` (15 NTD retail)
- Trading hours: day 08:45-13:45, night 15:00-05:00
- Tagged `r24_diagnostic` for easy filtering

**Runtime config** (`config/symbols.yaml` -- per-machine, gitignored):
The runtime config takes priority over base config. On the production host, this file
contains 42 OLD TXO entries at strikes 32700-34700 (from when TAIEX was ~33000).
These must be **replaced** with the new R24 entries. See Step 2b below.

Total symbols after change: 56 existing + 22 TXO = **78** (well under MAX_SUBSCRIPTIONS=200).

### 2. Diagnostic Script (`research/experiments/validations/r24_txo_tick_diagnostic.py`)

Standalone script for initial validation. Features:
- Lists available TXO contracts from Shioaji to help debug code mismatches
- Subscribes to configurable strike range around ATM
- Counts trade ticks vs quote events per symbol
- Extrapolates to daily rate
- Evaluates kill gate (>100 trade ticks/day)
- Optional JSON output via `TXO_OUTPUT_JSON` env var

---

## Deployment Steps

### Step 1: Run Diagnostic (Market Hours Required)

Before deploying to the persistent feed collector, validate with the diagnostic script:

```bash
# On the trading host (${REMOTE_USER}@${REMOTE_HOST}:~/subhft or local)
cd ~/hft_platform  # or ~/subhft

# Set credentials (DO NOT put in command line on shared hosts)
export SHIOAJI_API_KEY="..."
export SHIOAJI_SECRET_KEY="..."

# Run 30-min observation (default)
# Adjust TXO_ATM_ESTIMATE if TAIEX has moved significantly from 23000
TXO_ATM_ESTIMATE=23000 TXO_OBSERVE_MINUTES=30 TXO_OUTPUT_JSON=txo_diag.json \
    uv run python research/experiments/validations/r24_txo_tick_diagnostic.py
```

**Evaluate results:**
- If extrapolated daily ticks > 100: PROCEED to Step 2
- If extrapolated daily ticks < 100: STOP. Direction B data quality insufficient.
- If 0 ticks received: Check contract codes, market hours, ATM estimate

### Step 2a: Update Runtime Config on Production Host

The production host uses `config/symbols.yaml` (per-machine, gitignored) which takes
priority over `config/base/symbols.yaml`. It currently has 42 OLD TXO entries at
strikes 32700-34700 with NO metadata (no underlying, strike, right, expiry).

**On the production host**, replace old TXO entries with R24 entries:

```bash
# Generate the replacement script
uv run python -c "
import yaml
with open('config/symbols.yaml') as f:
    data = yaml.safe_load(f)
# Remove old TXO entries
data['symbols'] = [s for s in data['symbols'] if not str(s.get('code','')).startswith('TXO')]
# Add new R24 entries from base config
with open('config/base/symbols.yaml') as f:
    base = yaml.safe_load(f)
r24_txo = [s for s in base['symbols'] if s.get('exchange') == 'OPT']
data['symbols'].extend(r24_txo)
with open('config/symbols.yaml', 'w') as f:
    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
print(f'Updated: removed old TXO, added {len(r24_txo)} R24 TXO entries')
"
```

### Step 2b: Verify Config Locally

```bash
# Lint check
uv run ruff check config/

# Verify symbols.yaml parses correctly
uv run python -c "
import yaml
with open('config/base/symbols.yaml') as f:
    data = yaml.safe_load(f)
symbols = data['symbols']
txo = [s for s in symbols if s.get('exchange') == 'OPT']
print(f'Total symbols: {len(symbols)}')
print(f'TXO options: {len(txo)}')
for s in txo[:3]:
    print(f'  {s[\"code\"]}: strike={s.get(\"strike\")}, right={s.get(\"right\")}')
"
```

Expected output:
```
Total symbols: 78
TXO options: 22
  TXO22500D6: strike=22500, right=C
  TXO22500P6: strike=22500, right=P
  TXO22600D6: strike=22600, right=C
```

### Step 3: Verify InstrumentRegistry Population

```bash
uv run python -c "
from hft_platform.feed_adapter.normalizer import SymbolMetadata
sm = SymbolMetadata()
from hft_platform.core.instrument_registry import InstrumentType
opts = [p for p in sm.registry._profiles.values()
        if p.instrument_type == InstrumentType.OPTION]
print(f'Option profiles registered: {len(opts)}')
for p in opts[:3]:
    print(f'  {p.symbol}: type={p.instrument_type.value}, '
          f'strike={p.strike_scaled}, right={p.option_right}, '
          f'expiry={p.expiry}, multiplier={p.multiplier}')
"
```

Expected: 22 option profiles with correct metadata.

### Step 4: Deploy to Production Feed Collector

```bash
# On the remote host
ssh ${REMOTE_USER}@${REMOTE_HOST}

# Pull latest config
cd ~/subhft
git pull origin main  # or copy symbols.yaml manually

# Restart the feed collector (deployment is manual per ops policy)
# The exact restart command depends on the deployment setup:
docker compose restart hft-engine
# OR if running via systemd:
# sudo systemctl restart hft-engine
```

### Step 5: Monitor (First 30 Minutes)

After deployment, verify TXO data is flowing:

```bash
# Check logs for TXO subscription success
docker compose logs hft-engine 2>&1 | grep -i "txo\|OPT\|option" | head -20

# Check ClickHouse for incoming TXO data (wait 5+ minutes)
docker exec clickhouse clickhouse-client --query "
  SELECT
    symbol,
    type,
    count() as cnt,
    min(toDateTime64(exch_ts/1e9, 3)) as first_ts,
    max(toDateTime64(exch_ts/1e9, 3)) as last_ts
  FROM hft.market_data
  WHERE symbol LIKE 'TXO%'
    AND toDate(exch_ts/1e9) = today()
  GROUP BY symbol, type
  ORDER BY cnt DESC
  LIMIT 20
"
```

Expected: Both `Tick` and `BidAsk` rows for TXO symbols. Tick rows will be ~0.3% of total (per R17 analysis).

### Step 6: Daily Monitoring Query

Run this daily for the first week to track accumulation:

```bash
docker exec clickhouse clickhouse-client --query "
  SELECT
    toDate(exch_ts/1e9) as day,
    countIf(type = 'Tick') as tick_count,
    countIf(type = 'BidAsk') as bidask_count,
    countIf(type = 'Tick' AND volume > 0) as trade_ticks,
    round(countIf(type = 'Tick') * 100.0 / count(), 2) as tick_pct,
    uniqExact(symbol) as symbols
  FROM hft.market_data
  WHERE symbol LIKE 'TXO%'
  GROUP BY day
  ORDER BY day DESC
  LIMIT 10
"
```

---

## Monthly Rollover Procedure

TXO contract codes include month/year suffixes. When the front-month expires:

1. Identify new front-month codes (e.g., April D6/P6 -> May E6/Q6)
2. Update strike levels if TAIEX has moved
3. Edit `config/base/symbols.yaml`: replace expired TXO entries with new month
4. Redeploy config
5. Verify new contracts subscribe successfully

**April 2026 expiry: 2026-04-15** (third Wednesday). Update symbols by 2026-04-14.

---

## Kill Gates (Data Accumulation Phase)

| Gate | Threshold | Action |
|------|-----------|--------|
| Day 1: diagnostic tick count | < 100 extrapolated daily | Kill Direction B permanently |
| Day 5: daily tick density | < 100/day on 3+ days | Kill Direction B |
| Day 20: accumulated trade ticks | < 2,000 total | Kill Direction B |
| Day 20: temporal overlap | TXO + futures data on < 15/20 days | Kill Direction B |

If all gates pass after 20 days, proceed to Direction B Stage 2 (options feature module).

---

## Rollback

To remove TXO subscription:
1. Delete all TXO entries from `config/base/symbols.yaml`
2. Redeploy config
3. TXO data already in ClickHouse is retained (no cleanup needed)
