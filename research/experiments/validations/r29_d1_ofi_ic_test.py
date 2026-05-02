"""R29 D1 Falsification: Cumulative OFI Incremental IC Test

Kill threshold: incremental IC < 0.02 (detrended) for cumulative OFI
over existing 300s EMA OFI, predicting forward returns at 1min-1hr.

Uses golden parquet L5 BidAsk data for TX futures contracts.
"""

import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

GOLDEN_DIR = Path("research/data/real/golden")
TX_SYMBOLS = ["TXFD6", "TXFB6", "TXFC6", "TXFE6"]

# OFI cumulation windows (seconds)
CUM_WINDOWS = [60, 300, 900]  # 1min, 5min, 15min
# Forward return horizons (seconds)
FWD_HORIZONS = [60, 300, 900, 1800, 3600]  # 1min, 5min, 15min, 30min, 1hr
# EMA decay for "existing 300s OFI" baseline
EMA_ALPHA_300S = 2.0 / (300.0 + 1)


def load_bidask_day(parquet_path: str) -> pd.DataFrame:
    """Load BidAsk events from golden parquet, extract L1 best bid/ask."""
    df = pd.read_parquet(parquet_path)
    ba = df[df["type"] == "BidAsk"].copy()
    if len(ba) == 0:
        return pd.DataFrame()

    # Extract L1 (first element of each L5 array)
    ba["best_bid_px"] = ba["bids_price"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_ask_px"] = ba["asks_price"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_bid_qty"] = ba["bids_vol"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_ask_qty"] = ba["asks_vol"].apply(lambda x: x[0] if len(x) > 0 else np.nan)

    ba = ba.dropna(subset=["best_bid_px", "best_ask_px"])

    # Mid price (raw, not scaled)
    ba["mid"] = (ba["best_bid_px"] + ba["best_ask_px"]) / 2.0

    # Time in seconds from epoch
    ba["ts_s"] = ba["exch_ts"].astype(float) / 1e9

    # Filter to regular session (approx 08:45 - 13:45 Taiwan = 00:45 - 05:45 UTC)
    # Actually timestamps are already in local or UTC, let's check by hour
    ba["hour"] = pd.to_datetime(ba["ts_s"], unit="s").dt.hour
    # Keep only reasonable trading hours (drop pre-open / after-hours)
    # TAIFEX regular: 08:45-13:45 CST = 00:45-05:45 UTC
    ba = ba[(ba["hour"] >= 0) & (ba["hour"] <= 6)]

    return ba[["ts_s", "best_bid_px", "best_ask_px", "best_bid_qty", "best_ask_qty", "mid"]].reset_index(drop=True)


def compute_ofi(ba: pd.DataFrame) -> pd.DataFrame:
    """Compute tick-level OFI following Cont-Kukanov-Stoikov (2010).

    OFI_t = Δ(bid_qty * I(bid_px >= prev_bid_px)) - Δ(ask_qty * I(ask_px <= prev_ask_px))
    Simplified: track bid/ask level changes and queue changes.
    """
    n = len(ba)
    ofi = np.zeros(n)

    for i in range(1, n):
        # Bid side
        if ba["best_bid_px"].iloc[i] > ba["best_bid_px"].iloc[i - 1]:
            bid_delta = ba["best_bid_qty"].iloc[i]
        elif ba["best_bid_px"].iloc[i] == ba["best_bid_px"].iloc[i - 1]:
            bid_delta = ba["best_bid_qty"].iloc[i] - ba["best_bid_qty"].iloc[i - 1]
        else:
            bid_delta = -ba["best_bid_qty"].iloc[i - 1]

        # Ask side
        if ba["best_ask_px"].iloc[i] < ba["best_ask_px"].iloc[i - 1]:
            ask_delta = ba["best_ask_qty"].iloc[i]
        elif ba["best_ask_px"].iloc[i] == ba["best_ask_px"].iloc[i - 1]:
            ask_delta = ba["best_ask_qty"].iloc[i] - ba["best_ask_qty"].iloc[i - 1]
        else:
            ask_delta = -ba["best_ask_qty"].iloc[i - 1]

        ofi[i] = bid_delta - ask_delta

    ba = ba.copy()
    ba["ofi"] = ofi
    return ba


def compute_features_and_returns(ba: pd.DataFrame) -> pd.DataFrame:
    """Compute cumulative OFI at multiple windows and forward returns.

    Resamples to 1-second bars for cleaner window alignment.
    """
    # Resample to 1-second resolution
    ba["ts_floor"] = np.floor(ba["ts_s"]).astype(int)
    grouped = ba.groupby("ts_floor").agg(
        ofi_sum=("ofi", "sum"),
        mid_last=("mid", "last"),
        count=("ofi", "count"),
    ).reset_index()

    if len(grouped) < 100:
        return pd.DataFrame()

    ts = grouped["ts_floor"].values
    ofi_1s = grouped["ofi_sum"].values
    mid = grouped["mid_last"].values
    n = len(grouped)

    result = {"ts": ts, "mid": mid}

    # Cumulative OFI at different windows (rolling sum)
    for w in CUM_WINDOWS:
        cum = np.full(n, np.nan)
        for i in range(w, n):
            cum[i] = ofi_1s[i - w:i].sum()
        result[f"cum_ofi_{w}s"] = cum

    # EMA OFI (300s baseline - approximates existing FeatureEngine 300s EMA)
    ema = np.full(n, np.nan)
    ema[0] = 0.0
    for i in range(1, n):
        ema[i] = EMA_ALPHA_300S * ofi_1s[i] + (1 - EMA_ALPHA_300S) * ema[i - 1]
    result["ema_ofi_300s"] = ema

    # Forward returns at different horizons
    for h in FWD_HORIZONS:
        fwd = np.full(n, np.nan)
        for i in range(n - h):
            if mid[i] > 0:
                fwd[i] = (mid[i + h] - mid[i]) / mid[i]
        result[f"fwd_ret_{h}s"] = fwd

    return pd.DataFrame(result)


def compute_rank_ic(signal: np.ndarray, ret: np.ndarray) -> float:
    """Spearman rank IC between signal and return."""
    mask = ~(np.isnan(signal) | np.isnan(ret))
    if mask.sum() < 30:
        return np.nan
    corr, _ = stats.spearmanr(signal[mask], ret[mask])
    return corr


def compute_detrended_ic(signal: np.ndarray, ret: np.ndarray, window: int = 300) -> float:
    """Detrended IC: compute IC on signal residuals after removing rolling mean."""
    mask = ~(np.isnan(signal) | np.isnan(ret))
    if mask.sum() < 100:
        return np.nan

    s = signal[mask]
    r = ret[mask]

    # Detrend signal with rolling mean
    s_series = pd.Series(s)
    s_detrended = s_series - s_series.rolling(window, min_periods=1).mean()

    corr, _ = stats.spearmanr(s_detrended.values, r)
    return corr


def main():
    all_results = []

    for sym in TX_SYMBOLS:
        sym_dir = GOLDEN_DIR / sym
        if not sym_dir.exists():
            continue

        parquets = sorted(sym_dir.glob("*.parquet"))
        print(f"\n{'='*60}")
        print(f"Symbol: {sym} ({len(parquets)} days)")
        print(f"{'='*60}")

        for pq in parquets:
            day = pq.stem
            print(f"\n  Processing {sym} {day}...")

            ba = load_bidask_day(str(pq))
            if len(ba) < 1000:
                print(f"    Skipped (only {len(ba)} events)")
                continue

            ba = compute_ofi(ba)
            features = compute_features_and_returns(ba)
            if len(features) < 500:
                print(f"    Skipped (only {len(features)} 1s bars)")
                continue

            print(f"    {len(ba)} BidAsk events -> {len(features)} 1s bars")

            # Compute IC for each signal x horizon
            for sig_name in [f"cum_ofi_{w}s" for w in CUM_WINDOWS] + ["ema_ofi_300s"]:
                for h in FWD_HORIZONS:
                    ret_name = f"fwd_ret_{h}s"
                    ic = compute_rank_ic(features[sig_name].values, features[ret_name].values)
                    ic_det = compute_detrended_ic(features[sig_name].values, features[ret_name].values)

                    all_results.append({
                        "symbol": sym,
                        "day": day,
                        "signal": sig_name,
                        "horizon": f"{h}s",
                        "rank_ic": ic,
                        "detrended_ic": ic_det,
                    })

    if not all_results:
        print("\nNo results computed!")
        sys.exit(1)

    results_df = pd.DataFrame(all_results)

    # Aggregate: mean IC across days per signal x horizon
    print("\n" + "=" * 80)
    print("AGGREGATE RESULTS: Mean IC across all days/symbols")
    print("=" * 80)

    agg = results_df.groupby(["signal", "horizon"]).agg(
        mean_ic=("rank_ic", "mean"),
        std_ic=("rank_ic", "std"),
        mean_det_ic=("detrended_ic", "mean"),
        std_det_ic=("detrended_ic", "std"),
        n_days=("rank_ic", "count"),
    ).reset_index()

    # Pivot for readability
    for metric_name, metric_col in [("Raw Rank IC", "mean_ic"), ("Detrended IC", "mean_det_ic")]:
        print(f"\n--- {metric_name} ---")
        pivot = agg.pivot(index="signal", columns="horizon", values=metric_col)
        # Reorder columns
        col_order = [f"{h}s" for h in FWD_HORIZONS if f"{h}s" in pivot.columns]
        pivot = pivot[col_order]
        print(pivot.to_string(float_format=lambda x: f"{x:.4f}"))

    # Key question: incremental IC of cumulative OFI over EMA 300s baseline
    print("\n" + "=" * 80)
    print("INCREMENTAL IC: cum_ofi vs ema_ofi_300s baseline")
    print("=" * 80)

    baseline = agg[agg["signal"] == "ema_ofi_300s"][["horizon", "mean_det_ic"]].set_index("horizon")

    for w in CUM_WINDOWS:
        sig = f"cum_ofi_{w}s"
        row = agg[agg["signal"] == sig][["horizon", "mean_det_ic"]].set_index("horizon")
        print(f"\n  {sig}:")
        for h in [f"{x}s" for x in FWD_HORIZONS]:
            if h in row.index and h in baseline.index:
                cum_ic = row.loc[h, "mean_det_ic"]
                base_ic = baseline.loc[h, "mean_det_ic"]
                incr = cum_ic - base_ic
                verdict = "PASS" if abs(incr) >= 0.02 else "FAIL"
                print(f"    {h}: cum={cum_ic:.4f} base={base_ic:.4f} incr={incr:+.4f} [{verdict}]")

    # Overall verdict
    print("\n" + "=" * 80)
    print("KILL GATE VERDICT")
    print("=" * 80)

    best_incr = 0.0
    best_combo = ""
    for w in CUM_WINDOWS:
        sig = f"cum_ofi_{w}s"
        for h in [f"{x}s" for x in FWD_HORIZONS]:
            row = agg[(agg["signal"] == sig) & (agg["horizon"] == h)]
            base = agg[(agg["signal"] == "ema_ofi_300s") & (agg["horizon"] == h)]
            if len(row) > 0 and len(base) > 0:
                incr = abs(row["mean_det_ic"].iloc[0]) - abs(base["mean_det_ic"].iloc[0])
                if incr > best_incr:
                    best_incr = incr
                    best_combo = f"{sig} -> {h}"

    print(f"  Best incremental detrended IC: {best_incr:+.4f} ({best_combo})")
    if best_incr >= 0.02:
        print("  VERDICT: PASS — cumulative OFI adds meaningful predictive power")
    else:
        print("  VERDICT: FAIL — cumulative OFI does NOT add incremental IC >= 0.02 over 300s EMA baseline")
        print("  Recommendation: KILL D1 or find a genuinely different signal formulation")


if __name__ == "__main__":
    main()
