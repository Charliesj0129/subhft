"""R31-04: Cross-stock momentum/reversal at intraday timescales.
At 1min-30min horizons, do stocks exhibit momentum or reversal cross-sectionally?
Can we predict laggards from leaders within sectors?
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# Sector groupings
FINANCIALS = ["2881", "2882", "2884", "2886", "2891", "2892", "2801"]
TECH = ["2330", "2317", "2303", "2308", "2454", "2382", "2379", "2395", "3034", "3008"]
TRADITIONAL = ["1101", "1102", "1216", "1301", "1303", "1326", "1402", "2002"]

ALL_STOCKS = sorted(set([d.name for d in DATA.iterdir() if d.is_dir() and d.name.isdigit()]))

# Common dates
date_sets = []
for sym in ALL_STOCKS[:30]:  # Use top 30 by availability
    dates = set(f.stem for f in (DATA / sym).glob("*.parquet"))
    if dates:
        date_sets.append(dates)
common_dates = sorted(set.intersection(*date_sets)) if date_sets else []
print(f"Common dates across 30 stocks: {len(common_dates)}")

def load_resampled_prices(sym, date_str, bar_ns):
    """Load and resample to regular bar_ns grid, return mid prices."""
    fp = DATA / sym / f"{date_str}.parquet"
    if not fp.exists():
        return None, None
    df = pd.read_parquet(fp)
    ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
    if len(ticks) < 50:
        return None, None

    ts = ticks["exch_ts"].values
    px = ticks["price_scaled"].values.astype(float)

    # Create grid
    t0, t1 = ts.min(), ts.max()
    grid = np.arange(t0, t1, bar_ns)
    if len(grid) < 10:
        return None, None

    # Forward-fill resample
    idx = np.searchsorted(ts, grid, side="right") - 1
    idx = np.clip(idx, 0, len(px) - 1)
    prices = px[idx]
    return grid, prices


# === 1. Cross-sectional momentum/reversal ===
# For each bar interval, compute: correlation between ret[t] and ret[t+1] across stocks
print("\n=== CROSS-SECTIONAL MOMENTUM/REVERSAL ===")
for bar_min in [1, 5, 10, 30]:
    bar_ns = bar_min * 60 * 1_000_000_000
    all_autocorr = []
    all_cross_pred_ic = []

    for date_str in common_dates:
        # Load all stocks for this date
        stock_rets = {}
        common_grid = None
        for sym in ALL_STOCKS:
            grid, prices = load_resampled_prices(sym, date_str, bar_ns)
            if prices is None:
                continue
            ret = np.diff(np.log(prices))
            ret[~np.isfinite(ret)] = 0
            stock_rets[sym] = ret
            if common_grid is None:
                common_grid = len(ret)
            else:
                common_grid = min(common_grid, len(ret))

        if len(stock_rets) < 10 or common_grid < 5:
            continue

        # Trim to common length
        for sym in stock_rets:
            stock_rets[sym] = stock_rets[sym][:common_grid]

        syms = list(stock_rets.keys())
        ret_matrix = np.array([stock_rets[s] for s in syms])  # shape: (n_stocks, n_bars)

        # Cross-sectional rank IC: rank of ret[t] vs rank of ret[t+1]
        for t in range(ret_matrix.shape[1] - 1):
            r_now = ret_matrix[:, t]
            r_next = ret_matrix[:, t + 1]
            # Spearman rank correlation
            from scipy.stats import spearmanr
            ic, pval = spearmanr(r_now, r_next)
            if np.isfinite(ic):
                all_cross_pred_ic.append(ic)

        # Individual stock autocorrelation
        for sym in syms:
            ac = np.corrcoef(stock_rets[sym][:-1], stock_rets[sym][1:])[0, 1]
            if np.isfinite(ac):
                all_autocorr.append(ac)

    if all_cross_pred_ic:
        ic_arr = np.array(all_cross_pred_ic)
        ac_arr = np.array(all_autocorr)
        print(f"\n  {bar_min:2d}-min bars:")
        print(f"    Cross-sectional predictive IC: mean={ic_arr.mean():.4f}, std={ic_arr.std():.4f}, "
              f"t-stat={ic_arr.mean()/ic_arr.std()*np.sqrt(len(ic_arr)):.2f}, n={len(ic_arr)}")
        print(f"    Avg individual autocorrelation: mean={ac_arr.mean():.4f}, std={ac_arr.std():.4f}")
        print(f"    IC > 0 (momentum): {(ic_arr > 0).mean():.1%}")
        print(f"    IC < 0 (reversal): {(ic_arr < 0).mean():.1%}")


# === 2. Sector leader-laggard analysis (Financials) ===
print("\n\n=== SECTOR LEADER-LAGGARD (FINANCIALS) ===")
# For each 1-min bar, identify the fastest mover and check if others follow
bar_ns_1m = 60 * 1_000_000_000

for date_str in common_dates[:3]:  # Show 3 dates
    print(f"\n  Date: {date_str}")
    sector_rets = {}
    min_len = None
    for sym in FINANCIALS:
        grid, prices = load_resampled_prices(sym, date_str, bar_ns_1m)
        if prices is None:
            continue
        ret = np.diff(np.log(prices))
        ret[~np.isfinite(ret)] = 0
        sector_rets[sym] = ret
        if min_len is None:
            min_len = len(ret)
        else:
            min_len = min(min_len, len(ret))

    if len(sector_rets) < 3 or min_len < 20:
        continue

    for sym in sector_rets:
        sector_rets[sym] = sector_rets[sym][:min_len]

    syms = list(sector_rets.keys())
    ret_matrix = np.array([sector_rets[s] for s in syms])

    # Cross-correlation matrix at lag 1
    n_stocks = len(syms)
    print(f"    Cross-correlation at lag=1 (row leads column):")
    print(f"    {'':8s}", end="")
    for s in syms:
        print(f" {s:>6s}", end="")
    print()

    for i, si in enumerate(syms):
        print(f"    {si:8s}", end="")
        for j, sj in enumerate(syms):
            if i == j:
                # Autocorrelation
                ac = np.corrcoef(ret_matrix[i, :-1], ret_matrix[i, 1:])[0, 1]
                print(f" {ac:6.3f}", end="")
            else:
                # si[t] -> sj[t+1]
                c = np.corrcoef(ret_matrix[i, :-1], ret_matrix[j, 1:])[0, 1]
                print(f" {c:6.3f}", end="")
        print()


# === 3. Intraday momentum factor (long winners, short losers) ===
print("\n\n=== INTRADAY CROSS-SECTIONAL STRATEGY P&L ===")
# At each rebalance point, go long stocks that went up, short stocks that went down
# Check cumulative P&L after costs

for bar_min in [5, 10, 30]:
    bar_ns = bar_min * 60 * 1_000_000_000
    strategy_pnl = []  # per-bar P&L

    for date_str in common_dates:
        stock_rets = {}
        min_len = None
        for sym in ALL_STOCKS:
            grid, prices = load_resampled_prices(sym, date_str, bar_ns)
            if prices is None:
                continue
            ret = np.diff(np.log(prices))
            ret[~np.isfinite(ret)] = 0
            stock_rets[sym] = ret
            if min_len is None:
                min_len = len(ret)
            else:
                min_len = min(min_len, len(ret))

        if len(stock_rets) < 10 or min_len < 5:
            continue

        for sym in stock_rets:
            stock_rets[sym] = stock_rets[sym][:min_len]

        syms = list(stock_rets.keys())
        ret_matrix = np.array([stock_rets[s] for s in syms])

        for t in range(min_len - 1):
            ranks = ret_matrix[:, t].argsort().argsort()  # rank
            n = len(syms)
            # Long top quintile, short bottom quintile
            weights = (ranks - ranks.mean()) / ranks.std() / n
            pnl = np.dot(weights, ret_matrix[:, t + 1])
            strategy_pnl.append(pnl)

    if strategy_pnl:
        pnl_arr = np.array(strategy_pnl) * 10000  # in bps
        sharpe = pnl_arr.mean() / pnl_arr.std() * np.sqrt(252 * (270 / bar_min)) if pnl_arr.std() > 0 else 0
        print(f"\n  {bar_min:2d}-min momentum strategy:")
        print(f"    Mean return: {pnl_arr.mean():.3f} bps/bar")
        print(f"    Std:         {pnl_arr.std():.3f} bps/bar")
        print(f"    Sharpe (ann): {sharpe:.2f}")
        print(f"    Win rate:    {(pnl_arr > 0).mean():.1%}")
        print(f"    N bars:      {len(pnl_arr)}")
        # After costs (5.85 bps RT per rebalance, but only partial turnover)
        est_turnover_cost = 5.85 * 0.4  # ~40% turnover per rebalance
        print(f"    Est cost/bar: {est_turnover_cost:.1f} bps")
        print(f"    Net return:  {pnl_arr.mean() - est_turnover_cost:.3f} bps/bar")
