"""R29 D3 Falsification: Exhaustion + Refill Reversal Reliability

Kill threshold: < 55% reliability of reversal >= 3 points after exhaustion+refill detected.

Exhaustion definition:
  - OFI EMA was strongly positive/negative (>1.5σ) and then crosses back toward zero
  - Depth on depleted side begins recovering (deep_depth_momentum reverses sign)

Simplified proxy with available data:
  - Detect periods where cumulative OFI was extreme then reverts
  - Measure subsequent price reversal
"""

import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

GOLDEN_DIR = Path("research/data/real/golden")
TX_SYMBOLS = ["TXFD6", "TXFB6", "TXFC6", "TXFE6"]

# Parameters
LOOKBACK_S = 300  # 5 min lookback for "extreme OFI" detection
CONFIRM_S = 60    # 1 min confirmation for "OFI reverting"
OFI_EXTREME_SIGMA = 1.5  # threshold for extreme OFI
OFI_REVERT_FRAC = 0.5    # OFI must drop to 50% of peak to confirm exhaustion


def load_and_compute(parquet_path: str) -> pd.DataFrame:
    """Load BidAsk, compute OFI, resample to 1s bars."""
    df = pd.read_parquet(parquet_path)
    ba = df[df["type"] == "BidAsk"].copy()
    if len(ba) < 1000:
        return pd.DataFrame()

    ba["best_bid_px"] = ba["bids_price"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_ask_px"] = ba["asks_price"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_bid_qty"] = ba["bids_vol"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba["best_ask_qty"] = ba["asks_vol"].apply(lambda x: x[0] if len(x) > 0 else np.nan)
    ba = ba.dropna(subset=["best_bid_px", "best_ask_px"])

    ba["mid"] = (ba["best_bid_px"] + ba["best_ask_px"]) / 2.0
    ba["spread"] = ba["best_ask_px"] - ba["best_bid_px"]
    ba["ts_s"] = ba["exch_ts"].astype(float) / 1e9

    # Filter trading hours
    ba["hour"] = pd.to_datetime(ba["ts_s"], unit="s").dt.hour
    ba = ba[(ba["hour"] >= 0) & (ba["hour"] <= 6)]

    if len(ba) < 1000:
        return pd.DataFrame()

    # Compute OFI
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

    ba = ba.copy()
    ba["ofi"] = ofi

    # Resample to 1s
    ba["ts_floor"] = np.floor(ba["ts_s"]).astype(int)
    bars = ba.groupby("ts_floor").agg(
        ofi_sum=("ofi", "sum"),
        mid_last=("mid", "last"),
        spread_mean=("spread", "mean"),
        bid_qty_last=("best_bid_qty", "last"),
        ask_qty_last=("best_ask_qty", "last"),
        n_events=("ofi", "count"),
    ).reset_index()

    return bars


def detect_exhaustion_events(bars: pd.DataFrame) -> list[dict]:
    """Detect exhaustion+refill events.

    Pattern:
    1. Rolling 5-min cumulative OFI reaches extreme (>1.5σ)
    2. Then decays to <50% of peak within next 1 min
    3. This is the "exhaustion" signal

    We measure if price subsequently reverses in the opposite direction.
    """
    ts = bars["ts_floor"].values
    ofi_1s = bars["ofi_sum"].values
    mid = bars["mid_last"].values
    n = len(bars)

    if n < LOOKBACK_S + CONFIRM_S + 3600:
        return []

    # Rolling cumulative OFI over 5-min windows
    cum_ofi = np.full(n, np.nan)
    for i in range(LOOKBACK_S, n):
        cum_ofi[i] = ofi_1s[i - LOOKBACK_S:i].sum()

    # Statistics for thresholding
    valid = cum_ofi[~np.isnan(cum_ofi)]
    if len(valid) < 100:
        return []
    ofi_mean = np.mean(valid)
    ofi_std = np.std(valid)
    if ofi_std < 1e-10:
        return []

    threshold_pos = ofi_mean + OFI_EXTREME_SIGMA * ofi_std
    threshold_neg = ofi_mean - OFI_EXTREME_SIGMA * ofi_std

    events = []
    cooldown = 0

    for i in range(LOOKBACK_S + CONFIRM_S, n - 3600):
        if cooldown > 0:
            cooldown -= 1
            continue

        c = cum_ofi[i - CONFIRM_S]  # OFI at start of confirmation window
        c_now = cum_ofi[i]          # OFI now

        if np.isnan(c) or np.isnan(c_now):
            continue

        # Check if OFI was extreme and has reverted
        is_exhaustion = False
        direction = 0

        if c > threshold_pos and c_now < c * OFI_REVERT_FRAC:
            # Was strongly positive, now reverted → exhaustion of buying
            is_exhaustion = True
            direction = -1  # expect downward reversal
        elif c < threshold_neg and c_now > c * OFI_REVERT_FRAC:
            # Was strongly negative, now reverted → exhaustion of selling
            is_exhaustion = True
            direction = 1  # expect upward reversal

        if is_exhaustion:
            # Measure reversal at various horizons
            entry_price = mid[i]
            event = {
                "ts": ts[i],
                "direction": direction,
                "ofi_peak": c,
                "ofi_now": c_now,
                "entry_mid": entry_price,
            }

            for h_name, h_s in [("1m", 60), ("5m", 300), ("15m", 900), ("30m", 1800)]:
                if i + h_s < n:
                    exit_price = mid[i + h_s]
                    signed_ret = direction * (exit_price - entry_price)
                    # Convert to approximate points (price is in scaled units, 1e6 per point)
                    signed_pts = signed_ret / 1e6
                    event[f"signed_ret_{h_name}"] = signed_ret
                    event[f"signed_pts_{h_name}"] = signed_pts
                else:
                    event[f"signed_ret_{h_name}"] = np.nan
                    event[f"signed_pts_{h_name}"] = np.nan

            events.append(event)
            cooldown = CONFIRM_S  # prevent overlapping events

    return events


def main():
    all_events = []
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
            bars = load_and_compute(str(pq))
            if len(bars) < 500:
                print(f"  {day}: skipped ({len(bars)} bars)")
                continue

            events = detect_exhaustion_events(bars)
            print(f"  {day}: {len(bars)} 1s bars, {len(events)} exhaustion events")

            for e in events:
                e["symbol"] = sym
                e["day"] = day

            all_events.extend(events)
            day_count += 1

    if not all_events:
        print("\nNo exhaustion events found!")
        sys.exit(1)

    edf = pd.DataFrame(all_events)
    n_events = len(edf)

    print(f"\n{'='*80}")
    print(f"EXHAUSTION EVENT STATISTICS")
    print(f"{'='*80}")
    print(f"  Total events: {n_events}")
    print(f"  Trading days: {day_count}")
    print(f"  Events per day: {n_events / day_count:.1f}")
    print(f"  Direction split: +1={len(edf[edf['direction']==1])}, -1={len(edf[edf['direction']==-1])}")

    # Reversal reliability
    print(f"\n{'='*80}")
    print(f"REVERSAL RELIABILITY (signed P&L in direction of expected reversal)")
    print(f"{'='*80}")

    for h_name in ["1m", "5m", "15m", "30m"]:
        col = f"signed_pts_{h_name}"
        if col not in edf.columns:
            continue

        vals = edf[col].dropna()
        if len(vals) < 5:
            print(f"\n  {h_name}: insufficient data ({len(vals)})")
            continue

        mean_pts = vals.mean()
        std_pts = vals.std()
        hit_3pts = (vals >= 3.0).sum() / len(vals)  # fraction reversing ≥3 pts
        hit_0pts = (vals > 0).sum() / len(vals)     # fraction reversing at all
        median_pts = vals.median()

        # t-test vs zero
        t_stat, p_val = stats.ttest_1samp(vals.values, 0)

        print(f"\n  {h_name} (n={len(vals)}):")
        print(f"    Mean: {mean_pts:+.2f} pts, Median: {median_pts:+.2f} pts, Std: {std_pts:.2f}")
        print(f"    Win rate (>0 pts): {hit_0pts:.1%}")
        print(f"    Win rate (≥3 pts): {hit_3pts:.1%}")
        print(f"    t-stat: {t_stat:.2f}, p-value: {p_val:.3f}")

    # Kill gate
    print(f"\n{'='*80}")
    print(f"KILL GATE VERDICT")
    print(f"{'='*80}")

    # Check 5m and 15m horizons (most relevant for D3 as exit signal)
    pass_any = False
    for h_name in ["5m", "15m"]:
        col = f"signed_pts_{h_name}"
        if col in edf.columns:
            vals = edf[col].dropna()
            if len(vals) >= 5:
                hit_3 = (vals >= 3.0).sum() / len(vals)
                print(f"  {h_name}: reversal ≥3pts rate = {hit_3:.1%} (threshold: ≥55%)")
                if hit_3 >= 0.55:
                    pass_any = True

    if pass_any:
        print("  VERDICT: PASS — exhaustion+refill reliably predicts reversal")
    else:
        print("  VERDICT: FAIL — reversal reliability < 55% at key horizons")
        # Also check simpler metric: win rate
        for h_name in ["5m", "15m"]:
            col = f"signed_pts_{h_name}"
            if col in edf.columns:
                vals = edf[col].dropna()
                if len(vals) >= 5:
                    wr = (vals > 0).sum() / len(vals)
                    print(f"  {h_name} simple win rate: {wr:.1%}")


if __name__ == "__main__":
    main()
