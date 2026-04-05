"""R31-03: Spread, volume, and intraday pattern analysis for all 49 stocks.
Which stocks have tight spreads? What are intraday volume/volatility patterns?
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# Get all stock symbols (4 digit numeric)
all_syms = sorted([d.name for d in DATA.iterdir()
                   if d.is_dir() and d.name.isdigit()])
print(f"Stock symbols: {len(all_syms)}")

def analyze_stock(sym):
    """Analyze spread, volume, tick count for a stock."""
    dates = sorted([f.stem for f in (DATA / sym).glob("*.parquet")])
    stats = []

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"]
        ba = df[df["type"] == "BidAsk"]

        if len(ticks) < 10 or len(ba) < 10:
            continue

        # Tick stats
        prices = ticks["price_scaled"].values
        volumes = ticks["volume"].values
        tick_count = len(ticks)
        total_volume = volumes.sum()
        avg_price = prices.mean()

        # Spread from BidAsk
        def safe_first(x):
            if isinstance(x, list) and len(x) > 0:
                return x[0]
            return np.nan

        best_bids = ba["bids_price"].apply(safe_first).values.astype(float)
        best_asks = ba["asks_price"].apply(safe_first).values.astype(float)
        spreads = best_asks - best_bids
        valid = (best_bids > 0) & (best_asks > 0) & (spreads > 0)
        if valid.sum() < 10:
            continue

        avg_spread = spreads[valid].mean()
        median_spread = np.median(spreads[valid])
        avg_mid = ((best_bids[valid] + best_asks[valid]) / 2).mean()
        spread_bps = avg_spread / avg_mid * 10000 if avg_mid > 0 else np.nan

        # Returns for volatility
        log_ret = np.diff(np.log(prices.astype(float)))
        log_ret = log_ret[np.isfinite(log_ret)]
        daily_vol = log_ret.std() * np.sqrt(len(log_ret)) if len(log_ret) > 10 else np.nan

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


# Analyze all stocks
results = {}
for sym in all_syms:
    df_stats = analyze_stock(sym)
    if df_stats is not None:
        results[sym] = df_stats

# Summary table
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

# Rank by MM viability: low spread/vol ratio, high tick count
print("\n\n=== TOP 15 MM-Viable Stocks (sorted by spread_bps, min 500 ticks/day) ===")
viable = [r for r in summary_rows if r["avg_ticks"] > 500]
viable.sort(key=lambda x: x["spread_bps"])
for r in viable[:15]:
    rt_cost_bps = 5.85 + 2 * r["spread_bps"]  # commission + crossing spread
    print(f"  {r['sym']:8s} spread={r['spread_bps']:.2f}bps, ticks/day={r['avg_ticks']:.0f}, "
          f"vol/day={r['avg_vol']:.0f}, est_RT_cost={rt_cost_bps:.1f}bps")

# Intraday volume pattern for top-5 liquid stocks
print("\n\n=== INTRADAY VOLUME PATTERN (30-min buckets) ===")
liquid_syms = [r["sym"] for r in sorted(viable, key=lambda x: -x["avg_vol"])[:5]]
for sym in liquid_syms:
    dates = sorted([f.stem for f in (DATA / sym).glob("*.parquet")])
    bucket_vols = {}
    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].copy()
        if len(ticks) < 100:
            continue
        # Convert ns to hour:minute
        ts_s = ticks["exch_ts"].values / 1e9
        # Bucket by 30-min intervals from session start
        t0 = ts_s.min()
        for _, row in ticks.iterrows():
            t = row["exch_ts"] / 1e9
            bucket = int((t - t0) / 1800)  # 30-min buckets
            bucket_vols[bucket] = bucket_vols.get(bucket, 0) + row["volume"]

    if bucket_vols:
        total = sum(bucket_vols.values())
        print(f"\n  {sym} (total vol across all days: {total})")
        for b in sorted(bucket_vols.keys()):
            pct = bucket_vols[b] / total * 100
            bar = "#" * int(pct * 2)
            print(f"    Bucket {b:2d} (t0+{b*30:3d}min): {pct:5.1f}% {bar}")
