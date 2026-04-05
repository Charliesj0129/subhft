"""R31-07: Deep dive on cross-sectional reversal — the strongest signal found.
At 1-min, cross-sectional IC = -0.23, t-stat = -47.7.
This means: stocks that went up in the last minute tend to go DOWN in the next minute (reversal).
A REVERSAL strategy (short winners, long losers) should be hugely profitable before costs.
Let's quantify the edge after costs.
"""
import pandas as pd
import numpy as np
from pathlib import Path
from scipy.stats import spearmanr
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")
ALL_STOCKS = sorted([d.name for d in DATA.iterdir() if d.is_dir() and d.name.isdigit()])

# Common dates
date_sets = []
for sym in ALL_STOCKS:
    dates = set(f.stem for f in (DATA / sym).glob("*.parquet"))
    if dates:
        date_sets.append(dates)
common_dates = sorted(set.intersection(*date_sets))
print(f"Common dates: {len(common_dates)}")

def load_resampled(sym, date_str, bar_ns):
    fp = DATA / sym / f"{date_str}.parquet"
    if not fp.exists():
        return None, None
    df = pd.read_parquet(fp)
    ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
    if len(ticks) < 50:
        return None, None
    ts = ticks["exch_ts"].values
    px = ticks["price_scaled"].values.astype(float)
    t0, t1 = ts.min(), ts.max()
    grid = np.arange(t0, t1, bar_ns)
    if len(grid) < 10:
        return None, None
    idx = np.searchsorted(ts, grid, side="right") - 1
    idx = np.clip(idx, 0, len(px) - 1)
    return grid, px[idx]


# === REVERSAL STRATEGY: short winners, long losers ===
# Use 1-min bars (strongest signal)
bar_ns = 60 * 1_000_000_000

print("\n=== 1-MIN CROSS-SECTIONAL REVERSAL STRATEGY ===")
print("Strategy: At each 1-min bar, rank stocks by return. "
      "Short top quintile, long bottom quintile next bar.\n")

daily_pnl = []
daily_turnover = []
daily_trades = []
all_bar_pnl = []

for date_str in common_dates:
    stock_rets = {}
    min_len = None
    for sym in ALL_STOCKS:
        grid, prices = load_resampled(sym, date_str, bar_ns)
        if prices is None:
            continue
        ret = np.diff(np.log(prices))
        ret[~np.isfinite(ret)] = 0
        stock_rets[sym] = ret
        if min_len is None:
            min_len = len(ret)
        else:
            min_len = min(min_len, len(ret))

    if len(stock_rets) < 20 or min_len < 10:
        continue

    syms = list(stock_rets.keys())
    for s in syms:
        stock_rets[s] = stock_rets[s][:min_len]

    n_stocks = len(syms)
    ret_matrix = np.array([stock_rets[s] for s in syms])  # (n_stocks, n_bars)

    # Quintile-based strategy
    n_q = max(1, n_stocks // 5)
    day_gross_pnl = 0
    day_trades = 0
    prev_weights = np.zeros(n_stocks)

    for t in range(min_len - 1):
        ranks = ret_matrix[:, t].argsort().argsort()
        # REVERSAL: short the winners (top quintile), long the losers (bottom quintile)
        weights = np.zeros(n_stocks)
        weights[ranks < n_q] = 1.0 / n_q  # long losers
        weights[ranks >= n_stocks - n_q] = -1.0 / n_q  # short winners

        # P&L: weights * next bar return
        pnl = np.dot(weights, ret_matrix[:, t + 1])
        all_bar_pnl.append(pnl * 10000)  # in bps

        # Turnover
        turnover = np.abs(weights - prev_weights).sum()
        day_trades += int((weights != 0).sum())
        day_gross_pnl += pnl
        prev_weights = weights

    daily_pnl.append(day_gross_pnl * 10000)  # bps
    daily_trades.append(day_trades)

bar_pnl = np.array(all_bar_pnl)
daily_pnl_arr = np.array(daily_pnl)

print(f"Per-bar stats (1-min):")
print(f"  Mean return:    {bar_pnl.mean():.2f} bps/bar")
print(f"  Std return:     {bar_pnl.std():.2f} bps/bar")
print(f"  Win rate:       {(bar_pnl > 0).mean():.1%}")
print(f"  Sharpe (ann):   {bar_pnl.mean() / bar_pnl.std() * np.sqrt(252 * 270):.2f}")
print(f"  N bars:         {len(bar_pnl)}")

print(f"\nPer-day stats:")
print(f"  Mean daily PnL: {daily_pnl_arr.mean():.1f} bps")
print(f"  Std daily PnL:  {daily_pnl_arr.std():.1f} bps")
print(f"  Sharpe (ann):   {daily_pnl_arr.mean() / daily_pnl_arr.std() * np.sqrt(252):.2f}" if daily_pnl_arr.std() > 0 else "  N/A")
for i, (d, p) in enumerate(zip(common_dates, daily_pnl)):
    print(f"  {d}: {p:.1f} bps")

# Cost analysis
# Each rebalance: trade ~2*n_q stocks. Cost = 5.85 bps per stock per RT.
# But we're doing L/S, so turnover per bar is roughly 2*(2*n_q)/n_stocks of portfolio
# With quintile, ~40% portfolio turns over each bar
turnover_per_bar = 0.4  # approximate
cost_per_bar = turnover_per_bar * 5.85  # bps
print(f"\nCost analysis:")
print(f"  Est turnover/bar: {turnover_per_bar*100:.0f}%")
print(f"  Est cost/bar:     {cost_per_bar:.1f} bps")
print(f"  Net return/bar:   {bar_pnl.mean() - cost_per_bar:.2f} bps")
print(f"  Gross/Cost ratio: {bar_pnl.mean() / cost_per_bar:.2f}x")

# === Check if this is just a bid-ask bounce artifact ===
print("\n\n=== BID-ASK BOUNCE CHECK ===")
print("Using mid prices from BidAsk data instead of tick prices")

# Rerun with mid prices
all_bar_pnl_mid = []
for date_str in common_dates[:4]:  # subset for speed
    stock_rets = {}
    min_len = None
    for sym in ALL_STOCKS:
        fp = DATA / sym / f"{date_str}.parquet"
        if not fp.exists():
            continue
        df = pd.read_parquet(fp)
        ba = df[df["type"] == "BidAsk"].sort_values("exch_ts")
        if len(ba) < 100:
            continue
        bp = ba["bids_price"].values
        ap = ba["asks_price"].values
        best_bid = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp], dtype=float)
        best_ask = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap], dtype=float)
        mid = (best_bid + best_ask) / 2
        valid = np.isfinite(mid) & (mid > 0)
        ts = ba["exch_ts"].values
        mid_v = mid[valid]
        ts_v = ts[valid]
        if len(mid_v) < 50:
            continue

        t0, t1 = ts_v.min(), ts_v.max()
        grid = np.arange(t0, t1, bar_ns)
        if len(grid) < 10:
            continue
        idx = np.searchsorted(ts_v, grid, side="right") - 1
        idx = np.clip(idx, 0, len(mid_v) - 1)
        prices = mid_v[idx]
        ret = np.diff(np.log(prices))
        ret[~np.isfinite(ret)] = 0
        stock_rets[sym] = ret
        if min_len is None:
            min_len = len(ret)
        else:
            min_len = min(min_len, len(ret))

    if len(stock_rets) < 20 or min_len < 10:
        continue

    syms = list(stock_rets.keys())
    for s in syms:
        stock_rets[s] = stock_rets[s][:min_len]
    n_stocks = len(syms)
    ret_matrix = np.array([stock_rets[s] for s in syms])
    n_q = max(1, n_stocks // 5)

    for t in range(min_len - 1):
        ranks = ret_matrix[:, t].argsort().argsort()
        weights = np.zeros(n_stocks)
        weights[ranks < n_q] = 1.0 / n_q
        weights[ranks >= n_stocks - n_q] = -1.0 / n_q
        pnl = np.dot(weights, ret_matrix[:, t + 1])
        all_bar_pnl_mid.append(pnl * 10000)

if all_bar_pnl_mid:
    mid_pnl = np.array(all_bar_pnl_mid)
    print(f"  Mid-price reversal: mean={mid_pnl.mean():.2f} bps, std={mid_pnl.std():.2f}, "
          f"win={( mid_pnl > 0).mean():.1%}, n={len(mid_pnl)}")
    print(f"  Compare tick-price: mean={bar_pnl[:len(mid_pnl)].mean():.2f} bps (same date range)")


# === TWSE constraint: no intraday short selling for most stocks ===
print("\n\n=== LONG-ONLY REVERSAL (TWSE short-sell constraint) ===")
print("TWSE restricts intraday short selling for most stocks.")
print("Recomputing with LONG-ONLY: buy losers, hold to next bar.\n")

long_only_pnl = []
for date_str in common_dates:
    stock_rets = {}
    min_len = None
    for sym in ALL_STOCKS:
        grid, prices = load_resampled(sym, date_str, bar_ns)
        if prices is None:
            continue
        ret = np.diff(np.log(prices))
        ret[~np.isfinite(ret)] = 0
        stock_rets[sym] = ret
        if min_len is None:
            min_len = len(ret)
        else:
            min_len = min(min_len, len(ret))

    if len(stock_rets) < 20 or min_len < 10:
        continue

    syms = list(stock_rets.keys())
    for s in syms:
        stock_rets[s] = stock_rets[s][:min_len]
    n_stocks = len(syms)
    ret_matrix = np.array([stock_rets[s] for s in syms])
    n_q = max(1, n_stocks // 5)

    for t in range(min_len - 1):
        ranks = ret_matrix[:, t].argsort().argsort()
        weights = np.zeros(n_stocks)
        weights[ranks < n_q] = 1.0 / n_q  # long only: buy the losers
        pnl = np.dot(weights, ret_matrix[:, t + 1])
        long_only_pnl.append(pnl * 10000)

lo_pnl = np.array(long_only_pnl)
print(f"  Long-only reversal: mean={lo_pnl.mean():.2f} bps/bar, std={lo_pnl.std():.2f}, "
      f"win={(lo_pnl > 0).mean():.1%}")
print(f"  After cost (5.85 bps RT): {lo_pnl.mean() - 5.85:.2f} bps/bar")
# But we need to consider: we only trade the losers, so cost is just 5.85 bps
# Actually more nuanced: we buy at bar t, sell at bar t+1, full RT = 5.85 bps
# But we hold overlapping positions... let's compute assuming full rebalance
# Only ~40% of bottom quintile changes per bar
print(f"  After cost (40% turnover): {lo_pnl.mean() - 5.85 * 0.4:.2f} bps/bar")
