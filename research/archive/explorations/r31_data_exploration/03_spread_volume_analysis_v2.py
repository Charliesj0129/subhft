"""R31-03v2: Spread, volume, and intraday pattern analysis for all 49 stocks.
Fixed: numpy arrays not lists for bid/ask data.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

all_syms = sorted([d.name for d in DATA.iterdir()
                   if d.is_dir() and d.name.isdigit()])
print(f"Stock symbols: {len(all_syms)}")

def analyze_stock(sym):
    dates = sorted([f.stem for f in (DATA / sym).glob("*.parquet")])
    stats = []

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"]
        ba = df[df["type"] == "BidAsk"]

        if len(ticks) < 10 or len(ba) < 10:
            continue

        prices = ticks["price_scaled"].values
        volumes = ticks["volume"].values
        tick_count = len(ticks)
        total_volume = int(volumes.sum())
        avg_price = float(prices.mean())

        # Spread from BidAsk — arrays not lists
        bp_col = ba["bids_price"].values
        ap_col = ba["asks_price"].values

        best_bids = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp_col], dtype=float)
        best_asks = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap_col], dtype=float)
        spreads = best_asks - best_bids
        valid = (best_bids > 0) & (best_asks > 0) & (spreads > 0)
        if valid.sum() < 10:
            continue

        avg_spread = float(spreads[valid].mean())
        median_spread = float(np.median(spreads[valid]))
        avg_mid = float(((best_bids[valid] + best_asks[valid]) / 2).mean())
        spread_bps = avg_spread / avg_mid * 10000 if avg_mid > 0 else np.nan

        log_ret = np.diff(np.log(prices.astype(float)))
        log_ret = log_ret[np.isfinite(log_ret)]
        daily_vol = float(log_ret.std() * np.sqrt(len(log_ret))) if len(log_ret) > 10 else np.nan

        stats.append({
            "date": date_str,
            "tick_count": tick_count,
            "total_volume": total_volume,
            "avg_price": avg_price,
            "avg_spread": avg_spread,
            "median_spread": median_spread,
            "spread_bps": spread_bps,
            "daily_vol": daily_vol,
        })

    if not stats:
        return None
    return pd.DataFrame(stats)

results = {}
for sym in all_syms:
    df_stats = analyze_stock(sym)
    if df_stats is not None:
        results[sym] = df_stats

# Summary
print(f"\n{'Symbol':8s} {'Days':>4s} {'AvgTicks':>10s} {'AvgVol':>12s} {'AvgPrice':>14s} "
      f"{'Spread(bps)':>12s} {'MedianSprd':>12s} {'DailyVol':>10s} {'Sprd/Vol':>10s}")
print("-" * 110)

summary_rows = []
for sym in sorted(results.keys()):
    s = results[sym]
    avg_ticks = s["tick_count"].mean()
    avg_vol = s["total_volume"].mean()
    avg_price = s["avg_price"].mean()
    avg_spread_bps = s["spread_bps"].mean()
    avg_median_sprd = s["median_spread"].mean()
    avg_daily_vol = s["daily_vol"].mean()
    sprd_over_vol = avg_spread_bps / avg_daily_vol if avg_daily_vol > 0 else np.nan

    summary_rows.append({
        "sym": sym,
        "days": len(s),
        "avg_ticks": avg_ticks,
        "avg_vol": avg_vol,
        "avg_price": avg_price,
        "spread_bps": avg_spread_bps,
        "median_spread": avg_median_sprd,
        "daily_vol": avg_daily_vol,
        "sprd_over_vol": sprd_over_vol,
    })

    print(f"{sym:8s} {len(s):4d} {avg_ticks:10.0f} {avg_vol:12.0f} {avg_price:14.0f} "
          f"{avg_spread_bps:12.2f} {avg_median_sprd:12.0f} {avg_daily_vol:10.4f} {sprd_over_vol:10.4f}")

# Rank by MM viability
print("\n\n=== TOP 15 MM-Viable Stocks (sorted by spread_bps, min 500 ticks/day) ===")
viable = [r for r in summary_rows if r["avg_ticks"] > 500]
viable.sort(key=lambda x: x["spread_bps"])
for r in viable[:15]:
    # Commission cost is ~5.85 bps RT; MM earns the spread minus cost
    # Effective spread earned = median_spread; cost = commission
    # Need spread_bps > 5.85 bps (commission) to be viable
    # But we also cross the spread, so we need spread > 2 * cost effectively
    print(f"  {r['sym']:8s} spread={r['spread_bps']:.2f}bps, ticks/day={r['avg_ticks']:.0f}, "
          f"vol/day={r['avg_vol']:.0f}, daily_vol={r['daily_vol']:.4f}")

# Also show widest-spread stocks (potential passive MM targets)
print("\n=== WIDEST SPREAD Stocks (passive MM opportunity) ===")
viable.sort(key=lambda x: -x["spread_bps"])
for r in viable[:10]:
    # For passive MM: you earn the spread, pay commission
    net_per_rt = r["spread_bps"] - 5.85  # earn spread, pay commission
    print(f"  {r['sym']:8s} spread={r['spread_bps']:.2f}bps, net_per_RT={net_per_rt:.2f}bps, "
          f"ticks/day={r['avg_ticks']:.0f}, vol/day={r['avg_vol']:.0f}")

# Intraday volume pattern for top-5 liquid stocks
print("\n\n=== INTRADAY VOLUME PATTERN (30-min buckets, top 5 by volume) ===")
liquid_syms = [r["sym"] for r in sorted(summary_rows, key=lambda x: -x["avg_vol"])[:5]]
print(f"Top 5 by volume: {liquid_syms}")

for sym in liquid_syms:
    dates = sorted([f.stem for f in (DATA / sym).glob("*.parquet")])
    bucket_counts = {}  # tick counts per bucket per day
    n_days = 0

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].copy()
        if len(ticks) < 100:
            continue
        n_days += 1

        ts_s = ticks["exch_ts"].values / 1e9
        tod_s = (ts_s + 8 * 3600) % 86400
        mins_from_9 = (tod_s - 9 * 3600) / 60

        for _, row in ticks.iterrows():
            t = row["exch_ts"] / 1e9
            tod = (t + 8 * 3600) % 86400
            m = (tod - 9 * 3600) / 60
            b = max(0, min(8, int(m / 30)))
            bucket_counts[b] = bucket_counts.get(b, 0) + int(row["volume"])

    if bucket_counts and n_days > 0:
        total = sum(bucket_counts.values())
        print(f"\n  {sym} ({n_days} days, total vol: {total:,})")
        for b in sorted(bucket_counts.keys()):
            pct = bucket_counts[b] / total * 100
            avg_vol = bucket_counts[b] / n_days
            label = f"{9 + b//2}:{(b%2)*30:02d}-{9 + (b+1)//2}:{((b+1)%2)*30:02d}"
            bar = "#" * int(pct * 2)
            print(f"    {label}: {pct:5.1f}% (avg {avg_vol:8.0f}/day) {bar}")
