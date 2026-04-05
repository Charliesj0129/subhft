"""R31-14: Sector lead-lag analysis with mid prices, continuous session only.
Financial sector (2881-2892) showed some interesting cross-correlations.
Check if there's a systematic leader-laggard pattern exploitable after costs.
"""
import pandas as pd
import numpy as np
from pathlib import Path
from scipy.stats import spearmanr
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

FINANCIALS = ["2881", "2882", "2884", "2886", "2891", "2892"]
LARGE_CAPS = ["2330", "2317", "2303", "2454", "2382"]

def filter_continuous(df):
    ts_s = df["exch_ts"].values / 1e9
    tod_s = (ts_s + 8 * 3600) % 86400
    mask = (tod_s >= 9 * 3600) & (tod_s <= 13.4167 * 3600)
    return df[mask].copy()

def load_mid_prices(sym, date_str, bar_ns):
    """Load mid prices from BidAsk, continuous session only, resampled to bar_ns grid."""
    fp = DATA / sym / f"{date_str}.parquet"
    if not fp.exists():
        return None, None
    df = pd.read_parquet(fp)
    df = filter_continuous(df)
    df = df.drop_duplicates(subset=["exch_ts", "type"], keep="first")
    ba = df[df["type"] == "BidAsk"].sort_values("exch_ts")
    if len(ba) < 100:
        return None, None

    bp = ba["bids_price"].values
    ap = ba["asks_price"].values
    best_bid = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp], dtype=float)
    best_ask = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap], dtype=float)
    mid = (best_bid + best_ask) / 2
    valid = np.isfinite(mid) & (mid > 0)
    ts = ba["exch_ts"].values[valid]
    mid = mid[valid]

    if len(mid) < 50:
        return None, None

    t0, t1 = ts.min(), ts.max()
    grid = np.arange(t0, t1, bar_ns)
    if len(grid) < 10:
        return None, None

    idx = np.searchsorted(ts, grid, side="right") - 1
    idx = np.clip(idx, 0, len(mid) - 1)
    return grid, mid[idx]


# Common dates
all_syms = FINANCIALS + LARGE_CAPS
date_sets = [set(f.stem for f in (DATA / s).glob("*.parquet")) for s in all_syms]
common_dates = sorted(set.intersection(*date_sets))
print(f"Common dates: {len(common_dates)}")

# === 1. Financial sector cross-correlations at 5-second resolution ===
print("\n=== FINANCIAL SECTOR CROSS-CORRELATION (5-second bars) ===")
bar_5s = 5 * 1_000_000_000

for date_str in common_dates[:4]:
    print(f"\n  {date_str}:")
    rets = {}
    min_len = None
    for sym in FINANCIALS:
        grid, prices = load_mid_prices(sym, date_str, bar_5s)
        if prices is None:
            continue
        r = np.diff(np.log(prices))
        r[~np.isfinite(r)] = 0
        rets[sym] = r
        if min_len is None:
            min_len = len(r)
        else:
            min_len = min(min_len, len(r))

    if len(rets) < 3 or min_len < 50:
        continue

    syms = list(rets.keys())
    for s in syms:
        rets[s] = rets[s][:min_len]

    # Print lag-1 cross-correlation matrix
    print(f"    Lag-1 cross-corr (row[t] -> col[t+1]):")
    print(f"    {'':8s}", end="")
    for s in syms:
        print(f" {s:>6s}", end="")
    print()

    for i, si in enumerate(syms):
        print(f"    {si:8s}", end="")
        for j, sj in enumerate(syms):
            c = np.corrcoef(rets[si][:-1], rets[sj][1:])[0, 1]
            print(f" {c:6.3f}", end="")
        print()


# === 2. Stock -> Index lead-lag with mid prices ===
print("\n\n=== STOCK -> TXF LEAD-LAG (mid prices, 1-second bars) ===")
bar_1s = 1_000_000_000  # 1 second

for sym in LARGE_CAPS:
    all_corrs = {lag: [] for lag in range(-10, 11)}

    for date_str in common_dates:
        grid_s, prices_s = load_mid_prices(sym, date_str, bar_1s)
        if prices_s is None:
            continue

        # TXF uses tick prices since it trades continuously
        fp = DATA / "TXFD6" / f"{date_str}.parquet"
        if not fp.exists():
            continue
        df = pd.read_parquet(fp)
        df = filter_continuous(df)
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
        if len(ticks) < 100:
            continue

        ts_f = ticks["exch_ts"].values
        px_f = ticks["price_scaled"].values.astype(float)

        # Resample to same 1-second grid
        t_min = max(grid_s[0], ts_f.min())
        t_max = min(grid_s[-1], ts_f.max())
        grid = np.arange(t_min, t_max, bar_1s)
        if len(grid) < 100:
            continue

        # Stock mid on grid
        idx_s = np.searchsorted(grid_s, grid, side="right") - 1
        idx_s = np.clip(idx_s, 0, len(prices_s) - 1)
        stock_p = prices_s[idx_s]

        # TXF on grid
        idx_f = np.searchsorted(ts_f, grid, side="right") - 1
        idx_f = np.clip(idx_f, 0, len(px_f) - 1)
        fut_p = px_f[idx_f]

        stock_ret = np.diff(np.log(stock_p))
        fut_ret = np.diff(np.log(fut_p))
        stock_ret[~np.isfinite(stock_ret)] = 0
        fut_ret[~np.isfinite(fut_ret)] = 0

        n = min(len(stock_ret), len(fut_ret))
        for lag in range(-10, 11):
            if lag >= 0:
                c = np.corrcoef(stock_ret[:n-lag], fut_ret[lag:n])[0, 1]
            else:
                c = np.corrcoef(stock_ret[-lag:n], fut_ret[:n+lag])[0, 1]
            if np.isfinite(c):
                all_corrs[lag].append(c)

    if all_corrs[0]:
        print(f"\n  {sym} -> TXF (1s bars, {len(all_corrs[0])} days):")
        for lag in range(-5, 6):
            if all_corrs[lag]:
                mean_c = np.mean(all_corrs[lag])
                # Positive lag = stock leads
                label = f"stock leads by {lag}s" if lag > 0 else (
                    f"TXF leads by {-lag}s" if lag < 0 else "contemporaneous")
                marker = " ***" if abs(mean_c) > 0.01 else ""
                print(f"    lag={lag:+3d} ({label:25s}): corr={mean_c:.5f}{marker}")


# === 3. Multi-stock signal for TXF ===
print("\n\n=== COMBINED STOCK SIGNAL -> TXF (1s bars) ===")
# Combine returns of top stocks as a composite signal
composite_ic = []

for date_str in common_dates:
    stock_rets_all = []
    fut_ret_arr = None

    for sym in LARGE_CAPS:
        grid_s, prices_s = load_mid_prices(sym, date_str, bar_1s)
        if prices_s is None:
            continue

        fp = DATA / "TXFD6" / f"{date_str}.parquet"
        if not fp.exists():
            continue
        df = pd.read_parquet(fp)
        df = filter_continuous(df)
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
        if len(ticks) < 100:
            continue

        ts_f = ticks["exch_ts"].values
        px_f = ticks["price_scaled"].values.astype(float)

        t_min = max(grid_s[0], ts_f.min())
        t_max = min(grid_s[-1], ts_f.max())
        grid = np.arange(t_min, t_max, bar_1s)
        if len(grid) < 100:
            continue

        idx_s = np.searchsorted(grid_s, grid, side="right") - 1
        idx_s = np.clip(idx_s, 0, len(prices_s) - 1)
        stock_p = prices_s[idx_s]

        idx_f = np.searchsorted(ts_f, grid, side="right") - 1
        idx_f = np.clip(idx_f, 0, len(px_f) - 1)
        fut_p = px_f[idx_f]

        sr = np.diff(np.log(stock_p))
        fr = np.diff(np.log(fut_p))
        sr[~np.isfinite(sr)] = 0
        fr[~np.isfinite(fr)] = 0

        n = min(len(sr), len(fr))
        stock_rets_all.append(sr[:n])
        if fut_ret_arr is None:
            fut_ret_arr = fr[:n]

    if len(stock_rets_all) >= 3 and fut_ret_arr is not None:
        # Simple average of stock returns as composite signal
        min_n = min(len(s) for s in stock_rets_all)
        composite = np.mean([s[:min_n] for s in stock_rets_all], axis=0)
        fut = fut_ret_arr[:min_n]

        # signal[t] -> TXF_ret[t+1] (stock composite leads TXF by 1 second)
        if len(composite) > 10:
            c = np.corrcoef(composite[:-1], fut[1:])[0, 1]
            if np.isfinite(c):
                composite_ic.append(c)

if composite_ic:
    ic_arr = np.array(composite_ic)
    t_stat = ic_arr.mean() / ic_arr.std() * np.sqrt(len(ic_arr)) if ic_arr.std() > 0 else 0
    print(f"  Composite stock signal -> TXF[t+1] (1s lag):")
    print(f"    mean IC = {ic_arr.mean():.6f}")
    print(f"    t-stat  = {t_stat:.2f}")
    print(f"    n_days  = {len(ic_arr)}")

    # Even at 36ms latency, 1-second signal should be executable
    # But need R^2 >> cost / vol
    r2 = ic_arr.mean() ** 2
    print(f"    R^2     = {r2:.10f}")
    print(f"    Edge estimate: IC * sigma * sqrt(bars/day)")
    # Rough: edge_per_bar = IC * sigma_1s * 1pt
    # TXF 1s vol ≈ 0.5 pts, 270*60=16200 bars/day
    # daily_edge ≈ IC * 0.5 * sqrt(16200)
    print(f"    TXF daily edge ≈ {ic_arr.mean() * 0.5 * np.sqrt(16200):.3f} pts")
    print(f"    TXF cost = 2 pts RT")
    print(f"    -> {'VIABLE' if ic_arr.mean() * 0.5 * np.sqrt(16200) > 4 else 'NOT VIABLE'}")
