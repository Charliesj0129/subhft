"""R31-02: Stock-index lead-lag analysis.
Do large-cap stocks (2330, 2317, 2303, 2454) lead TXF?
By how much? Is it tradeable after 36ms latency?
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# Large-cap stocks that are heavy TAIEX constituents
LEADERS = ["2330", "2317", "2303", "2454", "2382", "2412", "2308"]
INDEX_FUT = "TXFD6"

def load_ticks(sym, date_str):
    """Load tick data, return timestamp (ns) and mid_price."""
    fp = DATA / sym / f"{date_str}.parquet"
    if not fp.exists():
        return None
    df = pd.read_parquet(fp)
    ticks = df[df["type"] == "Tick"].copy()
    if len(ticks) < 100:
        return None
    ticks = ticks[["exch_ts", "price_scaled"]].copy()
    ticks.columns = ["ts", "price"]
    ticks = ticks.sort_values("ts").reset_index(drop=True)
    return ticks

def load_bidask_mid(sym, date_str):
    """Load BidAsk data, compute mid price."""
    fp = DATA / sym / f"{date_str}.parquet"
    if not fp.exists():
        return None
    df = pd.read_parquet(fp)
    ba = df[df["type"] == "BidAsk"].copy()
    if len(ba) < 100:
        return None
    # Extract best bid/ask from lists
    def get_best_bid(x):
        if isinstance(x, list) and len(x) > 0:
            return x[0]
        return np.nan
    def get_best_ask(x):
        if isinstance(x, list) and len(x) > 0:
            return x[0]
        return np.nan
    ba["best_bid"] = ba["bids_price"].apply(get_best_bid)
    ba["best_ask"] = ba["asks_price"].apply(get_best_ask)
    ba["mid"] = (ba["best_bid"] + ba["best_ask"]) / 2
    ba = ba[ba["mid"] > 0][["exch_ts", "mid"]].copy()
    ba.columns = ["ts", "mid"]
    ba = ba.sort_values("ts").reset_index(drop=True)
    return ba

def resample_to_grid(ts_arr, price_arr, grid_ns, method="ffill"):
    """Resample irregular tick data to regular grid."""
    idx = np.searchsorted(ts_arr, grid_ns, side="right") - 1
    idx = np.clip(idx, 0, len(price_arr) - 1)
    return price_arr[idx]

def compute_leadlag_corr(leader_ret, follower_ret, max_lag=20):
    """Compute cross-correlation at various lags. Positive lag = leader leads."""
    results = {}
    n = min(len(leader_ret), len(follower_ret))
    leader_ret = leader_ret[:n]
    follower_ret = follower_ret[:n]
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            c = np.corrcoef(leader_ret[:n-lag], follower_ret[lag:n])[0, 1]
        else:
            c = np.corrcoef(leader_ret[-lag:n], follower_ret[:n+lag])[0, 1]
        results[lag] = c
    return results

# Get common dates
stock_dates = set()
for f in (DATA / "2330").glob("*.parquet"):
    stock_dates.add(f.stem)
fut_dates = set()
for f in (DATA / INDEX_FUT).glob("*.parquet"):
    fut_dates.add(f.stem)
common_dates = sorted(stock_dates & fut_dates)
print(f"Common dates: {len(common_dates)}")

# Time grid: 100ms intervals
GRID_MS = 100  # 100ms bars
GRID_NS = GRID_MS * 1_000_000

all_results = {}

for date_str in common_dates:
    print(f"\n--- {date_str} ---")

    # Load TXF ticks
    fut_ticks = load_ticks(INDEX_FUT, date_str)
    if fut_ticks is None:
        print(f"  No TXF data")
        continue

    # Create time grid from TXF trading hours
    t_min = fut_ticks["ts"].min()
    t_max = fut_ticks["ts"].max()
    grid = np.arange(t_min, t_max, GRID_NS)

    # Resample TXF to grid
    fut_prices = resample_to_grid(
        fut_ticks["ts"].values, fut_ticks["price"].values.astype(float), grid
    )
    fut_ret = np.diff(np.log(fut_prices))
    fut_ret[~np.isfinite(fut_ret)] = 0

    for sym in LEADERS:
        stock_ticks = load_ticks(sym, date_str)
        if stock_ticks is None:
            continue

        # Resample stock to same grid
        stock_prices = resample_to_grid(
            stock_ticks["ts"].values, stock_ticks["price"].values.astype(float), grid
        )
        stock_ret = np.diff(np.log(stock_prices))
        stock_ret[~np.isfinite(stock_ret)] = 0

        # Cross-correlation: stock leads TXF?
        corrs = compute_leadlag_corr(stock_ret, fut_ret, max_lag=50)

        # Find peak correlation and its lag
        peak_lag = max(corrs, key=lambda k: abs(corrs[k]))
        peak_corr = corrs[peak_lag]

        # Correlation at specific lags of interest
        # lag>0 means stock_ret[t] correlates with fut_ret[t+lag] => stock leads
        key = (sym, date_str)
        all_results[key] = {
            "peak_lag": peak_lag,
            "peak_corr": peak_corr,
            "corr_lag0": corrs.get(0, 0),
            "corr_lag1": corrs.get(1, 0),  # stock leads by 100ms
            "corr_lag2": corrs.get(2, 0),  # stock leads by 200ms
            "corr_lag3": corrs.get(3, 0),  # stock leads by 300ms
            "corr_lag5": corrs.get(5, 0),  # stock leads by 500ms
            "corr_lag_m1": corrs.get(-1, 0),  # TXF leads by 100ms
            "corr_lag_m2": corrs.get(-2, 0),
        }

        print(f"  {sym}: peak_lag={peak_lag} ({peak_lag*GRID_MS}ms), peak_corr={peak_corr:.4f}, "
              f"lag0={corrs[0]:.4f}, lag+1={corrs[1]:.4f}, lag-1={corrs[-1]:.4f}")

# Summary across all dates
print("\n\n=== SUMMARY: Average cross-correlations (stock -> TXF) ===")
print(f"Grid: {GRID_MS}ms, positive lag = stock leads TXF")
print(f"{'Symbol':8s} {'N':>3s} {'lag0':>8s} {'lag+1':>8s} {'lag+2':>8s} {'lag+3':>8s} {'lag+5':>8s} {'lag-1':>8s} {'lag-2':>8s} {'peak_lag':>10s}")
for sym in LEADERS:
    sym_results = {k: v for k, v in all_results.items() if k[0] == sym}
    if not sym_results:
        continue
    n = len(sym_results)
    avg = {}
    for field in ["corr_lag0", "corr_lag1", "corr_lag2", "corr_lag3", "corr_lag5", "corr_lag_m1", "corr_lag_m2"]:
        avg[field] = np.mean([v[field] for v in sym_results.values()])
    avg_peak = np.mean([v["peak_lag"] for v in sym_results.values()])
    print(f"{sym:8s} {n:3d} {avg['corr_lag0']:8.4f} {avg['corr_lag1']:8.4f} {avg['corr_lag2']:8.4f} "
          f"{avg['corr_lag3']:8.4f} {avg['corr_lag5']:8.4f} {avg['corr_lag_m1']:8.4f} {avg['corr_lag_m2']:8.4f} "
          f"{avg_peak:10.1f}")

# Additional: Compute predictive R^2 at 100ms lag
print("\n=== Predictive R^2: stock_ret[t] -> fut_ret[t+1] (100ms forward) ===")
for sym in LEADERS:
    all_x, all_y = [], []
    for date_str in common_dates:
        fut_ticks = load_ticks(INDEX_FUT, date_str)
        stock_ticks = load_ticks(sym, date_str)
        if fut_ticks is None or stock_ticks is None:
            continue
        t_min = max(fut_ticks["ts"].min(), stock_ticks["ts"].min())
        t_max = min(fut_ticks["ts"].max(), stock_ticks["ts"].max())
        grid = np.arange(t_min, t_max, GRID_NS)
        if len(grid) < 100:
            continue
        fut_prices = resample_to_grid(fut_ticks["ts"].values, fut_ticks["price"].values.astype(float), grid)
        stock_prices = resample_to_grid(stock_ticks["ts"].values, stock_ticks["price"].values.astype(float), grid)
        fut_ret = np.diff(np.log(fut_prices))
        stock_ret = np.diff(np.log(stock_prices))
        # stock_ret[t] predicts fut_ret[t+1]
        x = stock_ret[:-1]
        y = fut_ret[1:]
        mask = np.isfinite(x) & np.isfinite(y)
        all_x.extend(x[mask])
        all_y.extend(y[mask])

    if len(all_x) < 100:
        print(f"  {sym}: insufficient data")
        continue

    all_x = np.array(all_x)
    all_y = np.array(all_y)
    corr = np.corrcoef(all_x, all_y)[0, 1]
    r2 = corr ** 2
    # t-stat
    n = len(all_x)
    t_stat = corr * np.sqrt(n - 2) / np.sqrt(1 - corr**2) if abs(corr) < 1 else 0
    print(f"  {sym}: corr={corr:.6f}, R^2={r2:.8f}, t-stat={t_stat:.2f}, n={n}")
