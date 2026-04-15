#!/usr/bin/env python3
"""
TMFD6 supplementary: regime-split summary (Jan-Feb wide spread vs Mar-Apr tight spread).
"""
import os, sys, urllib.request, urllib.parse, json

CH_HOST = "localhost"
CH_PORT = 8123
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
SYMBOL = "TMFD6"
SCALE = 1_000_000

def ch_query_json(sql):
    url = f"http://{CH_HOST}:{CH_PORT}/?" + urllib.parse.urlencode({
        "query": sql + " FORMAT JSONEachRow", "user": "default", "password": CH_PASSWORD
    })
    with urllib.request.urlopen(urllib.request.Request(url), timeout=120) as resp:
        text = resp.read().decode("utf-8").strip()
    return [json.loads(l) for l in text.split("\n") if l.strip()] if text else []

print("=" * 80)
print("  TMFD6 Regime-Split Analysis")
print("=" * 80)

# Regime 1: Jan-Feb (wide spreads)
# Regime 2: Mar 19 - Apr 8 (tight spreads, post regime change)
regimes = [
    ("Jan-Feb (wide)", "2026-01-01", "2026-02-28"),
    ("Mar-Apr (tight)", "2026-03-19", "2026-04-08"),
    ("Mar 19-Apr 8 only", "2026-03-19", "2026-04-08"),
]

for name, d1, d2 in regimes[:2]:
    rows = ch_query_json(f"""
        SELECT
            count() AS n,
            avg(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS avg_spread,
            median(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS med_spread,
            quantile(0.25)(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS q25_spread,
            quantile(0.75)(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS q75_spread,
            countIf(asks_price[1] - bids_price[1] = 1 * {SCALE}) * 100.0 / count() AS pct_1pt,
            countIf(asks_price[1] - bids_price[1] = 2 * {SCALE}) * 100.0 / count() AS pct_2pt,
            countIf(asks_price[1] - bids_price[1] = 3 * {SCALE}) * 100.0 / count() AS pct_3pt,
            countIf(asks_price[1] - bids_price[1] <= 3 * {SCALE}) * 100.0 / count() AS pct_lte3
        FROM hft.market_data
        WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
          AND toDate(toDateTime(exch_ts / 1000000000)) BETWEEN '{d1}' AND '{d2}'
          AND length(bids_price) >= 1 AND length(asks_price) >= 1
          AND bids_price[1] > 0 AND asks_price[1] > 0
    """)
    if rows:
        r = rows[0]
        print(f"\n  Regime: {name} ({d1} to {d2})")
        print(f"    Snapshots:    {int(r['n']):>12,}")
        print(f"    Avg spread:   {float(r['avg_spread']):>8.2f} pts")
        print(f"    Median:       {float(r['med_spread']):>8.2f} pts")
        print(f"    Q25/Q75:      {float(r['q25_spread']):>6.1f} / {float(r['q75_spread']):>6.1f} pts")
        print(f"    % at 1 pt:    {float(r['pct_1pt']):>6.1f}%")
        print(f"    % at 2 pt:    {float(r['pct_2pt']):>6.1f}%")
        print(f"    % at 3 pt:    {float(r['pct_3pt']):>6.1f}%")
        print(f"    % <= 3 pt:    {float(r['pct_lte3']):>6.1f}%")

# Focus analysis: Mar-Apr only (the current regime)
print("\n" + "=" * 80)
print("  Mar-Apr Focused Edge Analysis (Current Regime)")
print("=" * 80)

# What if we only trade when spread = 1 pt (the tightest)?
for spread_filter, label in [(1, "spread=1pt"), (2, "spread<=2pt"), (3, "spread<=3pt")]:
    if spread_filter == 1:
        cond = f"asks_price[1] - bids_price[1] = {SCALE}"
    else:
        cond = f"asks_price[1] - bids_price[1] <= {spread_filter * SCALE}"

    rows = ch_query_json(f"""
        SELECT
            count() AS n,
            avg(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS avg_spread,
            avg(bids_vol[1]) AS avg_bid_vol,
            avg(asks_vol[1]) AS avg_ask_vol
        FROM hft.market_data
        WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
          AND toDate(toDateTime(exch_ts / 1000000000)) BETWEEN '2026-03-19' AND '2026-04-08'
          AND length(bids_price) >= 1 AND length(asks_price) >= 1
          AND bids_price[1] > 0 AND asks_price[1] > 0
          AND {cond}
    """)
    if rows:
        r = rows[0]
        n = int(r["n"])
        avg_s = float(r["avg_spread"])
        half_s = avg_s / 2
        # For 1pt spread: if you're filled, you capture 0.5 pts (half spread)
        # But adverse selection still applies
        print(f"\n  Filter: {label}")
        print(f"    Matching snapshots:  {n:>10,}")
        print(f"    Avg spread:          {avg_s:>6.2f} pts")
        print(f"    Half-spread:         {half_s:>6.2f} pts")
        print(f"    Gross edge/RT:       {avg_s:>6.2f} pts (best case, full spread capture)")
        print(f"    Net edge/RT:         {avg_s - 4.0:>6.2f} pts (minus 4.0 RT cost)")
        print(f"    Avg L1 depth:        bid={float(r['avg_bid_vol']):.1f}  ask={float(r['avg_ask_vol']):.1f}")

# Tick-based volume analysis (how many trades per day?)
print("\n" + "=" * 80)
print("  Daily Trade Volume (Ticks)")
print("=" * 80)
rows = ch_query_json(f"""
    SELECT
        toDate(toDateTime(exch_ts / 1000000000)) AS day,
        count() AS tick_count,
        sum(volume) AS total_volume
    FROM hft.market_data
    WHERE symbol = '{SYMBOL}' AND type = 'Tick'
      AND toDate(toDateTime(exch_ts / 1000000000)) BETWEEN '2026-03-19' AND '2026-04-08'
    GROUP BY day
    ORDER BY day
""")
print(f"  {'Day':<12} {'Ticks':>10} {'Volume':>12}")
print(f"  {'-'*12} {'-'*10} {'-'*12}")
total_vol = 0
n_days = 0
for r in rows:
    tc = int(r["tick_count"])
    vol = int(r["total_volume"])
    total_vol += vol
    n_days += 1
    print(f"  {r['day']:<12} {tc:>10,} {vol:>12,}")
if n_days:
    print(f"\n  Avg daily volume: {total_vol / n_days:,.0f} contracts")

# Final comparison table
print("\n" + "=" * 80)
print("  TMFD6 vs TXFD6 Comparison (for reference)")
print("=" * 80)
print(f"  {'Metric':<30} {'TMFD6 (Mar-Apr)':>18} {'TXFD6 (known)':>18}")
print(f"  {'-'*30} {'-'*18} {'-'*18}")
print(f"  {'Point value':<30} {'10 NTD':>18} {'50 NTD':>18}")
print(f"  {'Avg spread (Mar-Apr)':<30} {'~2.85 pts':>18} {'~3 pts':>18}")
print(f"  {'RT cost':<30} {'4.0 pts':>18} {'4.68 pts':>18}")
print(f"  {'RT cost in NTD':<30} {'40 NTD':>18} {'234 NTD':>18}")
print(f"  {'Gross edge/RT':<30} {'~2.7 pts':>18} {'?':>18}")
print(f"  {'Net edge/RT':<30} {'~-1.3 pts':>18} {'?':>18}")
print(f"  {'Break-even comm/side':<30} {'6.7 NTD':>18} {'?':>18}")
print(f"  {'Verdict':<30} {'NOT VIABLE':>18} {'?':>18}")
