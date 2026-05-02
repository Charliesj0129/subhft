"""R29 D2 Falsification: Absorption Event Frequency Count

Kill threshold: < 500 absorption candidate events in available data.
Also measures conditional vs unconditional price move after detected events.

Absorption definition: 5-min window where:
  - Volume traded > 2σ above mean
  - |cumulative OFI| > 2σ above mean (strong directional flow)
  - |price change| < median spread (price barely moves despite flow)
"""

import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

GOLDEN_DIR = Path("research/data/real/golden")
TX_SYMBOLS = ["TXFD6", "TXFB6", "TXFC6", "TXFE6"]

# 5-min window size in seconds
WINDOW_S = 300
# Thresholds
VOLUME_SIGMA = 2.0
OFI_SIGMA = 2.0


def load_bidask_day(parquet_path: str) -> pd.DataFrame:
    """Load and parse BidAsk events."""
    df = pd.read_parquet(parquet_path)
    ba = df[df["type"] == "BidAsk"].copy()
    if len(ba) == 0:
        return pd.DataFrame()

    ba["best_bid_px"] = ba["bids_price"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_ask_px"] = ba["asks_price"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_bid_qty"] = ba["bids_vol"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_ask_qty"] = ba["asks_vol"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba = ba.dropna(subset=["best_bid_px", "best_ask_px"])

    ba["mid"] = (ba["best_bid_px"] + ba["best_ask_px"]) / 2.0
    ba["spread"] = ba["best_ask_px"] - ba["best_bid_px"]
    ba["ts_s"] = ba["exch_ts"].astype(float) / 1e9

    # Filter to trading hours
    ba["hour"] = pd.to_datetime(ba["ts_s"], unit="s").dt.hour
    ba = ba[(ba["hour"] >= 0) & (ba["hour"] <= 6)]

    return ba[["ts_s", "best_bid_px", "best_ask_px", "best_bid_qty", "best_ask_qty",
               "mid", "spread"]].reset_index(drop=True)


def compute_ofi_tick(ba: pd.DataFrame) -> np.ndarray:
    """Compute per-tick OFI."""
    n = len(ba)
    ofi = np.zeros(n)
    bid_px = ba["best_bid_px"].values
    ask_px = ba["best_ask_px"].values
    bid_qty = ba["best_bid_qty"].values
    ask_qty = ba["best_ask_qty"].values

    for i in range(1, n):
        if bid_px[i] > bid_px[i - 1]:
            bd = bid_qty[i]
        elif bid_px[i] == bid_px[i - 1]:
            bd = bid_qty[i] - bid_qty[i - 1]
        else:
            bd = -bid_qty[i - 1]

        if ask_px[i] < ask_px[i - 1]:
            ad = ask_qty[i]
        elif ask_px[i] == ask_px[i - 1]:
            ad = ask_qty[i] - ask_qty[i - 1]
        else:
            ad = -ask_qty[i - 1]

        ofi[i] = bd - ad

    return ofi


def detect_absorption_events(ba: pd.DataFrame) -> pd.DataFrame:
    """Detect absorption events in 5-min windows.

    Returns DataFrame with one row per 5-min window, flagging absorption candidates.
    """
    ofi = compute_ofi_tick(ba)
    ba = ba.copy()
    ba["ofi"] = ofi

    # Resample to 1-second bars
    ba["ts_floor"] = np.floor(ba["ts_s"]).astype(int)
    bars_1s = ba.groupby("ts_floor").agg(
        ofi_sum=("ofi", "sum"),
        mid_first=("mid", "first"),
        mid_last=("mid", "last"),
        spread_mean=("spread", "mean"),
        n_events=("ofi", "count"),
    ).reset_index()

    if len(bars_1s) < WINDOW_S * 2:
        return pd.DataFrame()

    # Create 5-min windows
    ts = bars_1s["ts_floor"].values
    start = ts[0]
    end = ts[-1]
    windows = []

    t = start
    while t + WINDOW_S <= end:
        mask = (bars_1s["ts_floor"] >= t) & (bars_1s["ts_floor"] < t + WINDOW_S)
        chunk = bars_1s[mask]
        if len(chunk) < 10:
            t += WINDOW_S
            continue

        vol = chunk["n_events"].sum()  # proxy for volume (# of BidAsk updates)
        cum_ofi = chunk["ofi_sum"].sum()
        price_change = abs(chunk["mid_last"].iloc[-1] - chunk["mid_first"].iloc[0])
        avg_spread = chunk["spread_mean"].mean()
        mid_start = chunk["mid_first"].iloc[0]
        mid_end = chunk["mid_last"].iloc[-1]

        # Forward returns at various horizons
        fwd = {}
        for h_name, h_s in [("5m", 300), ("15m", 900), ("30m", 1800), ("1h", 3600)]:
            fwd_mask = (bars_1s["ts_floor"] >= t + WINDOW_S) & (bars_1s["ts_floor"] < t + WINDOW_S + h_s)
            fwd_chunk = bars_1s[fwd_mask]
            if len(fwd_chunk) > 0:
                fwd_price = fwd_chunk["mid_last"].iloc[-1]
                fwd[f"fwd_ret_{h_name}"] = (fwd_price - mid_end) / mid_end if mid_end > 0 else np.nan
                # Signed by OFI direction
                ofi_sign = 1.0 if cum_ofi > 0 else -1.0
                fwd[f"fwd_signed_{h_name}"] = fwd[f"fwd_ret_{h_name}"] * ofi_sign
            else:
                fwd[f"fwd_ret_{h_name}"] = np.nan
                fwd[f"fwd_signed_{h_name}"] = np.nan

        windows.append({
            "ts": t,
            "volume": vol,
            "cum_ofi": cum_ofi,
            "abs_ofi": abs(cum_ofi),
            "price_change": price_change,
            "avg_spread": avg_spread,
            "mid_start": mid_start,
            **fwd,
        })

        t += WINDOW_S  # non-overlapping windows

    if not windows:
        return pd.DataFrame()

    wdf = pd.DataFrame(windows)

    # Compute thresholds
    vol_mean, vol_std = wdf["volume"].mean(), wdf["volume"].std()
    ofi_mean, ofi_std = wdf["abs_ofi"].mean(), wdf["abs_ofi"].std()

    wdf["vol_z"] = (wdf["volume"] - vol_mean) / (vol_std + 1e-10)
    wdf["ofi_z"] = (wdf["abs_ofi"] - ofi_mean) / (ofi_std + 1e-10)
    wdf["price_vs_spread"] = wdf["price_change"] / (wdf["avg_spread"] + 1e-10)

    # Absorption flag: high volume + high OFI + low price change
    wdf["is_absorption"] = (
        (wdf["vol_z"] > VOLUME_SIGMA) &
        (wdf["ofi_z"] > OFI_SIGMA) &
        (wdf["price_vs_spread"] < 1.0)  # price moved less than 1 spread
    )

    # Also flag with looser threshold for sensitivity analysis
    wdf["is_absorption_loose"] = (
        (wdf["vol_z"] > 1.5) &
        (wdf["ofi_z"] > 1.5) &
        (wdf["price_vs_spread"] < 1.5)
    )

    return wdf


def main():
    all_windows = []
    day_count = 0

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
            ba = load_bidask_day(str(pq))
            if len(ba) < 1000:
                print(f"  {day}: skipped ({len(ba)} events)")
                continue

            wdf = detect_absorption_events(ba)
            if len(wdf) == 0:
                print(f"  {day}: no 5-min windows")
                continue

            n_abs = wdf["is_absorption"].sum()
            n_abs_loose = wdf["is_absorption_loose"].sum()
            n_total = len(wdf)
            print(f"  {day}: {n_total} windows, {n_abs} strict absorptions, {n_abs_loose} loose absorptions")

            wdf["symbol"] = sym
            wdf["day"] = day
            all_windows.append(wdf)
            day_count += 1

    if not all_windows:
        print("\nNo data!")
        sys.exit(1)

    combined = pd.concat(all_windows, ignore_index=True)
    n_strict = combined["is_absorption"].sum()
    n_loose = combined["is_absorption_loose"].sum()
    n_total = len(combined)

    print(f"\n{'='*80}")
    print(f"ABSORPTION EVENT FREQUENCY")
    print(f"{'='*80}")
    print(f"  Total 5-min windows: {n_total}")
    print(f"  Trading days: {day_count}")
    print(f"  Strict absorption events (vol>2σ, OFI>2σ, ΔP<1 spread): {n_strict}")
    print(f"  Loose absorption events (vol>1.5σ, OFI>1.5σ, ΔP<1.5 spread): {n_loose}")
    print(f"  Strict per day: {n_strict / day_count:.1f}")
    print(f"  Loose per day: {n_loose / day_count:.1f}")

    # Conditional vs unconditional forward returns
    print(f"\n{'='*80}")
    print(f"CONDITIONAL vs UNCONDITIONAL FORWARD RETURNS (signed by OFI direction)")
    print(f"{'='*80}")

    for flag_name, flag_col in [("Strict", "is_absorption"), ("Loose", "is_absorption_loose")]:
        abs_events = combined[combined[flag_col]]
        non_events = combined[~combined[flag_col]]
        print(f"\n  --- {flag_name} absorption ({len(abs_events)} events) ---")

        for h in ["5m", "15m", "30m", "1h"]:
            col = f"fwd_signed_{h}"
            if col in abs_events.columns:
                cond = abs_events[col].dropna()
                uncond = combined[col].dropna()
                non = non_events[col].dropna()

                if len(cond) >= 5:
                    cond_mean = cond.mean()
                    uncond_mean = uncond.mean()
                    excess = cond_mean - uncond_mean

                    # Convert to approximate TX points (assume mid ~32000 * 1e6 scale)
                    # Price is in scaled units, 1 point = 1e6 in this data
                    # So return * mid ≈ price move in scaled units / 1e6 = points
                    avg_mid = combined["mid_start"].mean()
                    excess_pts = excess * avg_mid / 1e6 if avg_mid > 0 else 0

                    # t-test
                    if len(cond) >= 3 and len(non) >= 3:
                        t_stat, p_val = stats.ttest_ind(cond.values, non.values)
                    else:
                        t_stat, p_val = 0, 1

                    print(f"    {h}: cond={cond_mean:.6f} uncond={uncond_mean:.6f} "
                          f"excess={excess:+.6f} (~{excess_pts:+.1f} pts) "
                          f"t={t_stat:.2f} p={p_val:.3f} n={len(cond)}")
                else:
                    print(f"    {h}: insufficient data ({len(cond)} events)")

    # Kill gate
    print(f"\n{'='*80}")
    print(f"KILL GATE VERDICT")
    print(f"{'='*80}")
    print(f"  Strict events: {n_strict} (threshold: ≥500)")
    print(f"  Loose events: {n_loose}")

    if n_strict >= 500:
        print("  VERDICT: PASS — sufficient absorption events for Gate C")
    elif n_loose >= 500:
        print("  VERDICT: MARGINAL — loose threshold passes but strict doesn't")
        print("  Recommendation: proceed with loose definition, tighten later")
    else:
        print("  VERDICT: FAIL — insufficient events for statistical validity")
        if day_count > 0:
            needed_days = int(500 / max(n_loose / day_count, 0.1))
            print(f"  At current rate ({n_loose/day_count:.1f}/day loose), need ~{needed_days} days")
        print("  Recommendation: DEFER D2 until more data accumulates")


if __name__ == "__main__":
    main()
