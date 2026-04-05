"""R31-06: Intraday patterns — opening, lunch, close effects.
Longer-horizon patterns (1min-60min) since short-horizon is exhausted.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

LIQUID_STOCKS = ["2330", "2317", "2303", "2454", "2382", "2412", "2881", "2886"]

def ts_to_minutes_from_open(ts_ns):
    """Convert nanosecond timestamp to minutes from 9:00 AM (TWSE opens at 9:00)."""
    # TWSE trading: 9:00-13:30 (270 minutes)
    # ts is nanoseconds since epoch; extract time of day
    ts_s = ts_ns / 1e9
    # Get time of day in seconds (UTC+8)
    tod_s = (ts_s + 8 * 3600) % 86400
    # Minutes from 9:00 AM
    return (tod_s - 9 * 3600) / 60


# === 1. Opening 5-min return vs rest of day ===
print("=== OPENING 5-MIN RETURN VS REST-OF-DAY ===")
for sym in LIQUID_STOCKS:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    open5_rets = []
    rest_rets = []

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
        if len(ticks) < 100:
            continue

        prices = ticks["price_scaled"].values.astype(float)
        ts = ticks["exch_ts"].values
        mins = ts_to_minutes_from_open(ts)

        # First tick and tick at ~5 min
        mask_5 = mins <= 5
        mask_rest = mins > 5

        if mask_5.sum() < 5 or mask_rest.sum() < 5:
            continue

        p_open = prices[0]
        p_5min = prices[mask_5][-1]
        p_close = prices[-1]

        open5_ret = np.log(p_5min / p_open) if p_open > 0 and p_5min > 0 else np.nan
        rest_ret = np.log(p_close / p_5min) if p_5min > 0 and p_close > 0 else np.nan

        if np.isfinite(open5_ret) and np.isfinite(rest_ret):
            open5_rets.append(open5_ret)
            rest_rets.append(rest_ret)

    if len(open5_rets) >= 5:
        o5 = np.array(open5_rets)
        rest = np.array(rest_rets)
        # Correlation: does opening direction predict rest of day?
        corr = np.corrcoef(o5, rest)[0, 1]
        # Reversal strategy: opposite of opening direction
        rev_pnl = -np.sign(o5) * rest * 10000  # bps
        print(f"  {sym}: open5_corr_rest={corr:.3f}, rev_pnl_mean={rev_pnl.mean():.1f}bps, "
              f"rev_pnl_std={rev_pnl.std():.1f}bps, n={len(o5)}")


# === 2. Half-hour bucket returns — is there systematic drift? ===
print("\n\n=== HALF-HOUR BUCKET RETURNS (bps) ===")
print(f"{'Symbol':8s}", end="")
for b in range(9):  # 9 half-hour buckets in 4.5hr session
    print(f" {'B'+str(b):>7s}", end="")
print()

for sym in LIQUID_STOCKS:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    bucket_rets = {b: [] for b in range(9)}

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
        if len(ticks) < 100:
            continue

        prices = ticks["price_scaled"].values.astype(float)
        ts = ticks["exch_ts"].values
        mins = ts_to_minutes_from_open(ts)

        for b in range(9):
            t_start = b * 30
            t_end = (b + 1) * 30
            mask = (mins >= t_start) & (mins < t_end)
            bucket_prices = prices[mask]
            if len(bucket_prices) >= 2:
                ret = np.log(bucket_prices[-1] / bucket_prices[0])
                if np.isfinite(ret):
                    bucket_rets[b].append(ret)

    print(f"{sym:8s}", end="")
    for b in range(9):
        if bucket_rets[b]:
            mean_bps = np.mean(bucket_rets[b]) * 10000
            print(f" {mean_bps:7.1f}", end="")
        else:
            print(f" {'N/A':>7s}", end="")
    print()


# === 3. Lunch effect: pre-lunch volatility vs post-lunch ===
print("\n\n=== LUNCH EFFECT: Pre-lunch (11:30-12:00) vs Post-lunch (12:30-13:00) ===")
# TWSE: continuous trading 9:00-13:30. No actual lunch break, but volume typically dips.
for sym in LIQUID_STOCKS:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    pre_vols = []
    post_vols = []

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
        if len(ticks) < 100:
            continue

        prices = ticks["price_scaled"].values.astype(float)
        ts = ticks["exch_ts"].values
        mins = ts_to_minutes_from_open(ts)

        # Pre-lunch: 150-180 min from open (11:30-12:00)
        pre = prices[(mins >= 150) & (mins < 180)]
        # Post-lunch: 210-240 min from open (12:30-13:00)
        post = prices[(mins >= 210) & (mins < 240)]

        if len(pre) > 10 and len(post) > 10:
            pre_vol = np.std(np.diff(np.log(pre))) * 10000
            post_vol = np.std(np.diff(np.log(post))) * 10000
            if np.isfinite(pre_vol) and np.isfinite(post_vol) and pre_vol > 0:
                pre_vols.append(pre_vol)
                post_vols.append(post_vol)

    if pre_vols:
        print(f"  {sym}: pre_lunch_vol={np.mean(pre_vols):.2f}bps, post_lunch_vol={np.mean(post_vols):.2f}bps, "
              f"ratio={np.mean(post_vols)/np.mean(pre_vols):.2f}")


# === 4. VWAP reversion: Does price revert to VWAP? ===
print("\n\n=== VWAP REVERSION ANALYSIS ===")
for sym in LIQUID_STOCKS[:4]:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_ic = []

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts").reset_index(drop=True)
        if len(ticks) < 500:
            continue

        prices = ticks["price_scaled"].values.astype(float)
        volumes = ticks["volume"].values.astype(float)
        ts = ticks["exch_ts"].values

        # Compute rolling VWAP
        cum_pv = np.cumsum(prices * volumes)
        cum_v = np.cumsum(volumes)
        vwap = cum_pv / np.maximum(cum_v, 1)

        # Signal: deviation from VWAP
        deviation = (prices - vwap) / vwap  # positive = above VWAP

        # Forward return (50 ticks ahead)
        h = 50
        if len(prices) <= h + 10:
            continue

        fwd_ret = (prices[h:] - prices[:-h]) / prices[:-h]
        dev = deviation[:-h]

        valid = np.isfinite(fwd_ret) & np.isfinite(dev)
        if valid.sum() < 100:
            continue

        from scipy.stats import spearmanr
        ic, _ = spearmanr(dev[valid], fwd_ret[valid])
        if np.isfinite(ic):
            all_ic.append(ic)

    if all_ic:
        ic_arr = np.array(all_ic)
        t_stat = ic_arr.mean() / ic_arr.std() * np.sqrt(len(ic_arr)) if ic_arr.std() > 0 else 0
        print(f"  {sym}: mean_IC={ic_arr.mean():.4f}, t-stat={t_stat:.2f}, n_days={len(ic_arr)}")
        print(f"    {'REVERSAL' if ic_arr.mean() < 0 else 'MOMENTUM'} tendency")


# === 5. Opening auction patterns: First 5 min returns -> next 25 min ===
print("\n\n=== OPENING MOMENTUM/REVERSAL (5min -> 30min) ===")
for sym in LIQUID_STOCKS:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    signal_rets = []
    outcome_rets = []

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
        if len(ticks) < 100:
            continue

        prices = ticks["price_scaled"].values.astype(float)
        ts = ticks["exch_ts"].values
        mins = ts_to_minutes_from_open(ts)

        # Signal: return in first 5 min
        p0 = prices[0]
        m5 = prices[mins <= 5]
        m30 = prices[mins <= 30]

        if len(m5) < 5 or len(m30) < 5:
            continue

        p5 = m5[-1]
        p30 = m30[-1]

        r5 = np.log(p5 / p0) if p0 > 0 and p5 > 0 else np.nan
        r5_30 = np.log(p30 / p5) if p5 > 0 and p30 > 0 else np.nan

        if np.isfinite(r5) and np.isfinite(r5_30):
            signal_rets.append(r5)
            outcome_rets.append(r5_30)

    if len(signal_rets) >= 5:
        s = np.array(signal_rets)
        o = np.array(outcome_rets)
        corr = np.corrcoef(s, o)[0, 1]
        # Momentum: go with first 5 min direction
        mom_pnl = np.sign(s) * o * 10000  # bps
        print(f"  {sym}: corr(5min,5-30min)={corr:.3f}, "
              f"mom_pnl={mom_pnl.mean():.1f}bps +/- {mom_pnl.std():.1f}bps, n={len(s)}")
