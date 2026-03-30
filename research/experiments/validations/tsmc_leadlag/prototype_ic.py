"""
Round 17 Stage 2: TSMC (2330) → TMFD6 Lead-Lag IC Prototype

Tests whether TSMC stock signals predict Mini-TAIEX futures returns.
Offline research script — float is fine per Architecture Rule 11.

Signal Groups:
  1. Price Lead-Lag: 2330 past returns → TMFD6 forward returns
  2. Volume Surge: 2330 volume spikes → TMFD6 forward returns
  3. Spread/LOB State: 2330 spread/imbalance → TMFD6 direction/volatility

Kill Gate: IC < 0.02 at ALL horizons → dead.
"""

import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path
from scipy import stats

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent.parent.parent.parent.parent
DATA_2330 = BASE / "research" / "data" / "raw" / "2330"
DATA_TMFD6 = BASE / "research" / "data" / "raw" / "tmfd6"

DATES = ["2026-03-20", "2026-03-23", "2026-03-24"]
# Mar-19 TMFD6 has no day session; Mar-23 2330 ends early (partial day)

RESAMPLE_FREQ = "1s"  # 1-second bars for alignment

# Forward return horizons in seconds
HORIZONS = [1, 5, 30, 60, 300, 600]

# Lookback windows for 2330 past returns (seconds)
LOOKBACKS = [1, 5, 30, 60, 300]

# Day session filter — timestamps are UTC epoch nanos.
# Taiwan is UTC+8, so 09:00-13:30 TWN = 01:00-05:30 UTC.
DAY_START_H, DAY_START_M = 1, 0   # 09:00 TWN in UTC
DAY_END_H, DAY_END_M = 5, 30      # 13:30 TWN in UTC


def load_day(symbol_dir: Path, prefix: str, date_str: str) -> pd.DataFrame:
    """Load a single day's npy file into a DataFrame with datetime index."""
    fname = symbol_dir / f"{prefix}_{date_str}_l1.npy"
    if not fname.exists():
        return pd.DataFrame()
    arr = np.load(fname)
    df = pd.DataFrame(arr)
    df["ts"] = pd.to_datetime(df["local_ts"], unit="ns")
    df = df.set_index("ts").sort_index()
    return df


def filter_day_session(df: pd.DataFrame, date_str: str) -> pd.DataFrame:
    """Filter to 09:00-13:30 Taiwan day session."""
    dt = pd.Timestamp(date_str)
    start = dt.replace(hour=DAY_START_H, minute=DAY_START_M, second=0)
    end = dt.replace(hour=DAY_END_H, minute=DAY_END_M, second=0)
    return df.loc[start:end]


def resample_1s(df: pd.DataFrame) -> pd.DataFrame:
    """Resample to 1-second bars using last observation."""
    return df.resample(RESAMPLE_FREQ).last().dropna(subset=["mid_price"])


def compute_forward_returns(mid: pd.Series, horizon_s: int) -> pd.Series:
    """Compute forward log-returns at given horizon (in seconds)."""
    shift_periods = horizon_s  # 1-second bars → shift by horizon_s periods
    future = mid.shift(-shift_periods)
    return np.log(future / mid)


def compute_past_returns(mid: pd.Series, lookback_s: int) -> pd.Series:
    """Compute past log-returns over lookback window."""
    past = mid.shift(lookback_s)
    return np.log(mid / past)


def spearman_ic(signal: pd.Series, fwd_ret: pd.Series) -> tuple:
    """Compute Spearman rank IC and p-value."""
    valid = signal.notna() & fwd_ret.notna()
    if valid.sum() < 30:
        return np.nan, np.nan, 0
    s = signal[valid]
    r = fwd_ret[valid]
    ic, pval = stats.spearmanr(s, r)
    n = valid.sum()
    return ic, pval, n


def hit_rate(signal: pd.Series, fwd_ret: pd.Series) -> tuple:
    """Compute directional hit rate."""
    valid = signal.notna() & fwd_ret.notna() & (signal != 0) & (fwd_ret != 0)
    if valid.sum() < 30:
        return np.nan, 0
    s_sign = np.sign(signal[valid])
    r_sign = np.sign(fwd_ret[valid])
    hits = (s_sign == r_sign).mean()
    n = valid.sum()
    return hits, n


def newey_west_tstat(ic_values: list) -> float:
    """Simple Newey-West t-stat for IC series (lag=1)."""
    arr = np.array([x for x in ic_values if not np.isnan(x)])
    if len(arr) < 3:
        return np.nan
    mean_ic = np.mean(arr)
    n = len(arr)
    # Variance with Newey-West correction (1 lag)
    gamma0 = np.var(arr, ddof=1)
    gamma1 = np.cov(arr[:-1], arr[1:])[0, 1] if n > 2 else 0
    nw_var = gamma0 + 2 * (1 - 1 / (1 + 1)) * gamma1  # Bartlett weight
    if nw_var <= 0:
        return np.nan
    return mean_ic / np.sqrt(nw_var / n)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    results = {
        "signal_group_1": [],  # price lead-lag
        "signal_group_2": [],  # volume surge
        "signal_group_3": [],  # spread/LOB
    }

    all_aligned = []

    for date_str in DATES:
        print(f"\n{'='*60}")
        print(f"Processing {date_str}")
        print(f"{'='*60}")

        # Load data
        df_2330 = load_day(DATA_2330, "2330", date_str)
        df_tmfd6 = load_day(DATA_TMFD6, "TMFD6", date_str)

        if df_2330.empty or df_tmfd6.empty:
            print(f"  SKIP: missing data")
            continue

        # Filter day session
        df_2330 = filter_day_session(df_2330, date_str)
        df_tmfd6 = filter_day_session(df_tmfd6, date_str)

        print(f"  2330 day session: {len(df_2330)} ticks")
        print(f"  TMFD6 day session: {len(df_tmfd6)} ticks")

        if len(df_2330) < 100 or len(df_tmfd6) < 100:
            print(f"  SKIP: insufficient data")
            continue

        # Resample to 1-second bars
        bars_2330 = resample_1s(df_2330)
        bars_tmfd6 = resample_1s(df_tmfd6)

        print(f"  2330 1s bars: {len(bars_2330)}")
        print(f"  TMFD6 1s bars: {len(bars_tmfd6)}")

        # Align on common timestamps (inner join)
        common_idx = bars_2330.index.intersection(bars_tmfd6.index)
        if len(common_idx) < 100:
            print(f"  SKIP: only {len(common_idx)} common timestamps")
            continue

        aligned = pd.DataFrame(index=common_idx)
        aligned["mid_2330"] = bars_2330.loc[common_idx, "mid_price"]
        aligned["mid_tmfd6"] = bars_tmfd6.loc[common_idx, "mid_price"]
        aligned["spread_2330"] = bars_2330.loc[common_idx, "spread_bps"]
        aligned["spread_tmfd6"] = bars_tmfd6.loc[common_idx, "spread_bps"]
        aligned["bid_qty_2330"] = bars_2330.loc[common_idx, "bid_qty"]
        aligned["ask_qty_2330"] = bars_2330.loc[common_idx, "ask_qty"]
        aligned["volume_2330"] = bars_2330.loc[common_idx, "volume"]
        aligned["bid_qty_tmfd6"] = bars_tmfd6.loc[common_idx, "bid_qty"]
        aligned["ask_qty_tmfd6"] = bars_tmfd6.loc[common_idx, "ask_qty"]
        aligned["date"] = date_str
        aligned = aligned.dropna(subset=["mid_2330", "mid_tmfd6"])

        print(f"  Aligned bars: {len(aligned)}")
        all_aligned.append(aligned)

        # ------------------------------------------------------------------
        # Signal Group 1: Price Lead-Lag
        # ------------------------------------------------------------------
        print(f"\n  --- Signal Group 1: Price Lead-Lag ---")
        for lookback in LOOKBACKS:
            past_ret = compute_past_returns(aligned["mid_2330"], lookback)
            for horizon in HORIZONS:
                fwd_ret = compute_forward_returns(aligned["mid_tmfd6"], horizon)
                ic, pval, n = spearman_ic(past_ret, fwd_ret)
                hr, hr_n = hit_rate(past_ret, fwd_ret)
                results["signal_group_1"].append({
                    "date": date_str,
                    "lookback_s": lookback,
                    "horizon_s": horizon,
                    "ic": ic,
                    "ic_pval": pval,
                    "hit_rate": hr,
                    "n": n,
                })
                if abs(ic) > 0.01:
                    print(f"    LB={lookback:>3}s → H={horizon:>3}s: IC={ic:+.4f} (p={pval:.4f}), HR={hr:.3f}, n={n}")

        # ------------------------------------------------------------------
        # Signal Group 2: Volume Surge
        # ------------------------------------------------------------------
        print(f"\n  --- Signal Group 2: Volume Surge ---")
        # Volume is 0 everywhere in the data, so use bid_qty + ask_qty as activity proxy
        # OR use price change magnitude as proxy for trading activity
        # Since volume=0, compute "activity" as |price change| * (bid_qty + ask_qty)
        price_chg_2330 = aligned["mid_2330"].diff()
        total_depth_2330 = aligned["bid_qty_2330"] + aligned["ask_qty_2330"]

        # Use rolling median of total depth change as volume surge proxy
        depth_change = total_depth_2330.diff().abs()
        rolling_med = depth_change.rolling(120, min_periods=30).median()
        surge_mask = depth_change > 2 * rolling_med

        # Direction of price change during surge
        surge_direction = price_chg_2330.copy()
        surge_direction[~surge_mask] = np.nan

        for horizon in [30, 60, 300, 600]:
            fwd_ret = compute_forward_returns(aligned["mid_tmfd6"], horizon)
            ic, pval, n = spearman_ic(surge_direction, fwd_ret)
            hr, hr_n = hit_rate(surge_direction, fwd_ret)
            results["signal_group_2"].append({
                "date": date_str,
                "signal": "depth_surge_direction",
                "horizon_s": horizon,
                "ic": ic,
                "ic_pval": pval,
                "hit_rate": hr,
                "n": n,
                "surge_count": int(surge_mask.sum()),
            })
            print(f"    Depth surge dir → H={horizon:>3}s: IC={ic:+.4f}, HR={hr:.3f}, n={n}, surges={int(surge_mask.sum())}")

        # Also test: magnitude of 2330 price change (unsigned) → TMFD6 abs return
        abs_chg_2330 = price_chg_2330.abs()
        rolling_med_chg = abs_chg_2330.rolling(120, min_periods=30).median()
        big_move = abs_chg_2330 > 2 * rolling_med_chg

        for horizon in [30, 60, 300, 600]:
            fwd_abs_ret = compute_forward_returns(aligned["mid_tmfd6"], horizon).abs()
            # Does big 2330 move predict big TMFD6 move?
            ic, pval, n = spearman_ic(abs_chg_2330, fwd_abs_ret)
            results["signal_group_2"].append({
                "date": date_str,
                "signal": "abs_chg_magnitude",
                "horizon_s": horizon,
                "ic": ic,
                "ic_pval": pval,
                "hit_rate": np.nan,
                "n": n,
                "surge_count": int(big_move.sum()),
            })
            print(f"    |2330 chg| → |TMFD6 ret| H={horizon:>3}s: IC={ic:+.4f}, n={n}")

        # ------------------------------------------------------------------
        # Signal Group 3: Spread / LOB State
        # ------------------------------------------------------------------
        print(f"\n  --- Signal Group 3: Spread / LOB State ---")

        # 3a: 2330 spread change → TMFD6 forward volatility
        spread_chg = aligned["spread_2330"].diff()
        for horizon in [30, 60, 300, 600]:
            fwd_abs_ret = compute_forward_returns(aligned["mid_tmfd6"], horizon).abs()
            ic, pval, n = spearman_ic(spread_chg, fwd_abs_ret)
            results["signal_group_3"].append({
                "date": date_str,
                "signal": "spread_chg→vol",
                "horizon_s": horizon,
                "ic": ic,
                "ic_pval": pval,
                "n": n,
            })
            print(f"    Spread chg → |TMFD6| H={horizon:>3}s: IC={ic:+.4f}, n={n}")

        # 3b: 2330 bid-ask imbalance → TMFD6 direction
        imb_2330 = (aligned["bid_qty_2330"] - aligned["ask_qty_2330"]) / (
            aligned["bid_qty_2330"] + aligned["ask_qty_2330"]
        ).replace(0, np.nan)

        for horizon in HORIZONS:
            fwd_ret = compute_forward_returns(aligned["mid_tmfd6"], horizon)
            ic, pval, n = spearman_ic(imb_2330, fwd_ret)
            hr, hr_n = hit_rate(imb_2330, fwd_ret)
            results["signal_group_3"].append({
                "date": date_str,
                "signal": "imbalance→dir",
                "horizon_s": horizon,
                "ic": ic,
                "ic_pval": pval,
                "hit_rate": hr,
                "n": n,
            })
            if abs(ic) > 0.01:
                print(f"    2330 imb → TMFD6 H={horizon:>3}s: IC={ic:+.4f}, HR={hr:.3f}, n={n}")

        # 3c: 2330 spread level → TMFD6 forward abs return (wide spread = uncertainty)
        for horizon in [30, 60, 300, 600]:
            fwd_abs_ret = compute_forward_returns(aligned["mid_tmfd6"], horizon).abs()
            ic, pval, n = spearman_ic(aligned["spread_2330"], fwd_abs_ret)
            results["signal_group_3"].append({
                "date": date_str,
                "signal": "spread_level→vol",
                "horizon_s": horizon,
                "ic": ic,
                "ic_pval": pval,
                "n": n,
            })
            print(f"    2330 spread → |TMFD6| H={horizon:>3}s: IC={ic:+.4f}, n={n}")

    # ======================================================================
    # Pooled Analysis
    # ======================================================================
    print(f"\n{'='*60}")
    print("POOLED RESULTS ACROSS ALL DAYS")
    print(f"{'='*60}")

    if not all_aligned:
        print("NO DATA — cannot compute pooled results")
        return results

    pooled = pd.concat(all_aligned)
    print(f"Total aligned bars: {len(pooled)}")

    # ------------------------------------------------------------------
    # Pooled Signal Group 1: Price Lead-Lag
    # ------------------------------------------------------------------
    print(f"\n--- Pooled Signal Group 1: Price Lead-Lag ---")
    print(f"{'LB':>5} {'H':>5} {'IC':>8} {'p-val':>8} {'HR':>6} {'N':>8} {'t-stat':>8}")
    print("-" * 52)

    sg1_summary = []
    for lookback in LOOKBACKS:
        past_ret = compute_past_returns(pooled["mid_2330"], lookback)
        for horizon in HORIZONS:
            fwd_ret = compute_forward_returns(pooled["mid_tmfd6"], horizon)
            ic, pval, n = spearman_ic(past_ret, fwd_ret)
            hr, hr_n = hit_rate(past_ret, fwd_ret)

            # Collect per-day ICs for NW t-stat
            day_ics = []
            for date_str in DATES:
                day_rows = [r for r in results["signal_group_1"]
                            if r["date"] == date_str
                            and r["lookback_s"] == lookback
                            and r["horizon_s"] == horizon]
                if day_rows:
                    day_ics.append(day_rows[0]["ic"])

            nw_t = newey_west_tstat(day_ics) if len(day_ics) >= 2 else np.nan
            sg1_summary.append({
                "lookback_s": lookback, "horizon_s": horizon,
                "ic": ic, "pval": pval, "hr": hr, "n": n, "nw_tstat": nw_t,
            })
            marker = " ***" if abs(ic) >= 0.02 else " *" if abs(ic) >= 0.01 else ""
            print(f"{lookback:>5} {horizon:>5} {ic:>+8.4f} {pval:>8.4f} {hr:>6.3f} {n:>8}{marker}")

    # ------------------------------------------------------------------
    # Pooled Signal Group 2
    # ------------------------------------------------------------------
    print(f"\n--- Pooled Signal Group 2: Volume/Activity Surge ---")
    price_chg_2330 = pooled["mid_2330"].diff()
    total_depth_2330 = pooled["bid_qty_2330"] + pooled["ask_qty_2330"]
    depth_change = total_depth_2330.diff().abs()
    rolling_med = depth_change.rolling(120, min_periods=30).median()
    surge_mask = depth_change > 2 * rolling_med
    surge_direction = price_chg_2330.copy()
    surge_direction[~surge_mask] = np.nan

    print(f"{'Signal':>25} {'H':>5} {'IC':>8} {'HR':>6} {'N':>8}")
    print("-" * 55)

    for horizon in [30, 60, 300, 600]:
        fwd_ret = compute_forward_returns(pooled["mid_tmfd6"], horizon)
        ic, pval, n = spearman_ic(surge_direction, fwd_ret)
        hr, _ = hit_rate(surge_direction, fwd_ret)
        print(f"{'depth_surge_dir':>25} {horizon:>5} {ic:>+8.4f} {hr:>6.3f} {n:>8}")

    abs_chg_2330 = price_chg_2330.abs()
    for horizon in [30, 60, 300, 600]:
        fwd_abs_ret = compute_forward_returns(pooled["mid_tmfd6"], horizon).abs()
        ic, pval, n = spearman_ic(abs_chg_2330, fwd_abs_ret)
        print(f"{'|2330_chg|→|tmfd6|':>25} {horizon:>5} {ic:>+8.4f} {'  n/a':>6} {n:>8}")

    # ------------------------------------------------------------------
    # Pooled Signal Group 3
    # ------------------------------------------------------------------
    print(f"\n--- Pooled Signal Group 3: Spread / LOB State ---")
    spread_chg = pooled["spread_2330"].diff()
    imb_2330 = (pooled["bid_qty_2330"] - pooled["ask_qty_2330"]) / (
        pooled["bid_qty_2330"] + pooled["ask_qty_2330"]
    ).replace(0, np.nan)

    print(f"{'Signal':>25} {'H':>5} {'IC':>8} {'HR':>6} {'N':>8}")
    print("-" * 55)

    for horizon in [30, 60, 300, 600]:
        fwd_abs_ret = compute_forward_returns(pooled["mid_tmfd6"], horizon).abs()
        ic, pval, n = spearman_ic(spread_chg, fwd_abs_ret)
        print(f"{'spread_chg→vol':>25} {horizon:>5} {ic:>+8.4f} {'  n/a':>6} {n:>8}")

    for horizon in HORIZONS:
        fwd_ret = compute_forward_returns(pooled["mid_tmfd6"], horizon)
        ic, pval, n = spearman_ic(imb_2330, fwd_ret)
        hr, _ = hit_rate(imb_2330, fwd_ret)
        marker = " ***" if abs(ic) >= 0.02 else " *" if abs(ic) >= 0.01 else ""
        print(f"{'imbalance→dir':>25} {horizon:>5} {ic:>+8.4f} {hr:>6.3f} {n:>8}{marker}")

    for horizon in [30, 60, 300, 600]:
        fwd_abs_ret = compute_forward_returns(pooled["mid_tmfd6"], horizon).abs()
        ic, pval, n = spearman_ic(pooled["spread_2330"], fwd_abs_ret)
        print(f"{'spread_level→vol':>25} {horizon:>5} {ic:>+8.4f} {'  n/a':>6} {n:>8}")

    # ------------------------------------------------------------------
    # Kill Gate Check
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("KILL GATE CHECK")
    print(f"{'='*60}")

    max_abs_ic = 0
    best_signal = ""
    for row in sg1_summary:
        if abs(row["ic"]) > max_abs_ic:
            max_abs_ic = abs(row["ic"])
            best_signal = f"SG1 LB={row['lookback_s']}s H={row['horizon_s']}s"

    print(f"Max |IC| from SG1 (price lead-lag): {max_abs_ic:.4f} ({best_signal})")

    if max_abs_ic < 0.02:
        print(">>> KILL GATE: IC < 0.02 at ALL horizons. Direction is WEAK.")
        print("    But checking if any signal crosses threshold before final verdict...")
    else:
        print(f">>> PASS: IC >= 0.02 found. Signal has potential.")

    return results


if __name__ == "__main__":
    results = run()
