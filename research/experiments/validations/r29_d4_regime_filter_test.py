"""R29 D4 Falsification: Regime Filter OOS Improvement

Test whether opening 30-min features can predict which days a simple
momentum signal (OFI direction) will be profitable.

Walk-forward: leave-one-out cross-validation (given limited days).
Kill threshold: OOS Sharpe improvement < 0.3 over unfiltered baseline.

Simple base signal: sign of cumulative 5-min OFI → trade in that direction,
hold for 30min-1hr. The regime filter should improve this.
"""

import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

GOLDEN_DIR = Path("research/data/real/golden")
TX_SYMBOLS = ["TXFD6", "TXFB6", "TXFC6", "TXFE6"]


def load_and_compute_bars(parquet_path: str) -> pd.DataFrame:
    """Load BidAsk, compute OFI, return 1s bars."""
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

    ba["hour"] = pd.to_datetime(ba["ts_s"], unit="s").dt.hour
    ba = ba[(ba["hour"] >= 0) & (ba["hour"] <= 6)]
    if len(ba) < 1000:
        return pd.DataFrame()

    # OFI
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
    ba["ts_floor"] = np.floor(ba["ts_s"]).astype(int)

    bars = ba.groupby("ts_floor").agg(
        ofi_sum=("ofi", "sum"),
        mid_last=("mid", "last"),
        mid_first=("mid", "first"),
        spread_mean=("spread", "mean"),
        n_events=("ofi", "count"),
    ).reset_index()

    return bars


def compute_session_features(bars: pd.DataFrame) -> dict:
    """Compute features from first 30 minutes and full-session trading signals.

    Returns dict with opening features and trading P&L.
    """
    ts = bars["ts_floor"].values
    start_ts = ts[0]
    open_end = start_ts + 1800  # first 30 min

    # Opening features (first 30 min)
    open_bars = bars[bars["ts_floor"] < open_end]
    rest_bars = bars[bars["ts_floor"] >= open_end]

    if len(open_bars) < 100 or len(rest_bars) < 100:
        return None

    # Opening features
    open_ofi_cum = open_bars["ofi_sum"].sum()
    open_ofi_abs = abs(open_ofi_cum)
    open_volume = open_bars["n_events"].sum()
    open_mid_start = open_bars["mid_first"].iloc[0]
    open_mid_end = open_bars["mid_last"].iloc[-1]
    open_ret = (open_mid_end - open_mid_start) / open_mid_start if open_mid_start > 0 else 0
    open_abs_ret = abs(open_ret)

    # Opening volatility (1-min bar returns std)
    open_bars_5min = open_bars.copy()
    open_bars_5min["ts_5min"] = (open_bars_5min["ts_floor"] // 300) * 300
    open_5min = open_bars_5min.groupby("ts_5min").agg(mid=("mid_last", "last")).reset_index()
    if len(open_5min) >= 3:
        open_vol = np.std(np.diff(open_5min["mid"].values) / open_5min["mid"].values[:-1])
    else:
        open_vol = 0

    # Opening spread
    open_spread = open_bars["spread_mean"].mean()

    # Trading signals: simple OFI momentum on rest of session
    # Every 5 minutes, take OFI direction and hold for 30 min
    rest_1s = rest_bars.copy()
    trades = []

    # Generate 5-min OFI signals
    rest_1s["ts_5min"] = (rest_1s["ts_floor"] // 300) * 300
    bars_5min = rest_1s.groupby("ts_5min").agg(
        ofi_sum=("ofi_sum", "sum"),
        mid_start=("mid_first", "first"),
        mid_end=("mid_last", "last"),
    ).reset_index()

    for i in range(len(bars_5min)):
        ofi_sig = bars_5min["ofi_sum"].iloc[i]
        if abs(ofi_sig) < 1:  # no signal
            continue

        direction = 1 if ofi_sig > 0 else -1
        entry = bars_5min["mid_end"].iloc[i]

        # Look for 30-min forward exit (6 bars ahead)
        if i + 6 < len(bars_5min):
            exit_price = bars_5min["mid_end"].iloc[i + 6]
            pnl_pts = direction * (exit_price - entry) / 1e6  # convert to points
            trades.append(pnl_pts)

    if not trades:
        return None

    trades = np.array(trades)

    return {
        # Opening features
        "open_ofi_cum": open_ofi_cum,
        "open_ofi_abs": open_ofi_abs,
        "open_volume": open_volume,
        "open_abs_ret": open_abs_ret,
        "open_volatility": open_vol,
        "open_spread": open_spread,
        "open_direction": 1 if open_ofi_cum > 0 else -1,
        # Session P&L from base signal
        "n_trades": len(trades),
        "total_pnl_pts": trades.sum(),
        "mean_pnl_pts": trades.mean(),
        "win_rate": (trades > 0).sum() / len(trades),
        "trades": trades,  # raw trades for per-trade analysis
    }


def main():
    sessions = []
    day_count = 0

    for sym in TX_SYMBOLS:
        sym_dir = GOLDEN_DIR / sym
        if not sym_dir.exists():
            continue

        parquets = sorted(sym_dir.glob("*.parquet"))
        print(f"\n{'='*60}")
        print(f"Symbol: {sym}")
        print(f"{'='*60}")

        for pq in parquets:
            day = pq.stem
            bars = load_and_compute_bars(str(pq))
            if len(bars) < 500:
                print(f"  {day}: skipped")
                continue

            features = compute_session_features(bars)
            if features is None:
                print(f"  {day}: insufficient data for features")
                continue

            features["symbol"] = sym
            features["day"] = day
            print(f"  {day}: {features['n_trades']} trades, "
                  f"mean={features['mean_pnl_pts']:+.1f} pts, "
                  f"wr={features['win_rate']:.0%}, "
                  f"open_vol={features['open_volatility']:.6f}")
            sessions.append(features)
            day_count += 1

    if len(sessions) < 5:
        print(f"\nInsufficient sessions ({len(sessions)})")
        sys.exit(1)

    # Analyze regime filter effectiveness
    print(f"\n{'='*80}")
    print(f"REGIME FILTER ANALYSIS")
    print(f"{'='*80}")
    print(f"Total sessions: {len(sessions)}")

    # Base signal performance (no filter)
    all_trades = np.concatenate([s["trades"] for s in sessions])
    base_mean = all_trades.mean()
    base_std = all_trades.std()
    base_sharpe = base_mean / base_std * np.sqrt(252 * 10) if base_std > 0 else 0  # ~10 trades/day
    base_wr = (all_trades > 0).sum() / len(all_trades)

    print(f"\n  BASE SIGNAL (all days, no filter):")
    print(f"    Total trades: {len(all_trades)}")
    print(f"    Mean P&L: {base_mean:+.2f} pts/trade")
    print(f"    Std: {base_std:.2f} pts")
    print(f"    Win rate: {base_wr:.1%}")
    print(f"    Annualized Sharpe: {base_sharpe:.2f}")

    # Try various regime filters
    sdf = pd.DataFrame([{k: v for k, v in s.items() if k != "trades"} for s in sessions])

    print(f"\n  REGIME FEATURES SUMMARY:")
    for feat in ["open_ofi_abs", "open_volume", "open_abs_ret", "open_volatility", "open_spread"]:
        vals = sdf[feat].values
        print(f"    {feat}: mean={np.mean(vals):.4f}, std={np.std(vals):.4f}, "
              f"min={np.min(vals):.4f}, max={np.max(vals):.4f}")

    # Regime filter: split sessions by median of each feature
    print(f"\n  REGIME SPLIT BY OPENING FEATURES:")
    print(f"  (Split at median, compare 'trending' vs 'range-bound' halves)")

    best_improvement = 0.0
    best_feature = ""

    for feat in ["open_ofi_abs", "open_volume", "open_abs_ret", "open_volatility", "open_spread"]:
        median_val = sdf[feat].median()

        # "High" feature = more volatile/trending opening
        high_idx = sdf[feat] >= median_val
        low_idx = sdf[feat] < median_val

        high_trades = np.concatenate([sessions[i]["trades"] for i in range(len(sessions)) if high_idx.iloc[i]])
        low_trades = np.concatenate([sessions[i]["trades"] for i in range(len(sessions)) if low_idx.iloc[i]])

        if len(high_trades) < 10 or len(low_trades) < 10:
            continue

        high_mean = high_trades.mean()
        high_std = high_trades.std()
        high_sharpe = high_mean / high_std * np.sqrt(252 * 10) if high_std > 0 else 0

        low_mean = low_trades.mean()
        low_std = low_trades.std()
        low_sharpe = low_mean / low_std * np.sqrt(252 * 10) if low_std > 0 else 0

        # Best half Sharpe improvement over base
        best_half_sharpe = max(high_sharpe, low_sharpe)
        improvement = best_half_sharpe - base_sharpe

        print(f"\n    {feat} (median={median_val:.4f}):")
        print(f"      High half: mean={high_mean:+.2f} pts, sharpe={high_sharpe:.2f}, "
              f"n={len(high_trades)}, wr={((high_trades>0).sum()/len(high_trades)):.1%}")
        print(f"      Low half:  mean={low_mean:+.2f} pts, sharpe={low_sharpe:.2f}, "
              f"n={len(low_trades)}, wr={((low_trades>0).sum()/len(low_trades)):.1%}")
        print(f"      Best improvement over base: {improvement:+.2f}")

        if improvement > best_improvement:
            best_improvement = improvement
            best_feature = feat

    # Leave-one-out cross-validation
    print(f"\n{'='*80}")
    print(f"LEAVE-ONE-OUT CROSS-VALIDATION")
    print(f"{'='*80}")

    loo_filtered_trades = []
    loo_all_trades = []

    for i in range(len(sessions)):
        # Train on all except i
        train_sessions = [sessions[j] for j in range(len(sessions)) if j != i]
        test_session = sessions[i]

        # Best feature from in-sample analysis
        if best_feature:
            train_vals = [s[best_feature] for s in train_sessions]
            threshold = np.median(train_vals)

            # Determine which half is better in training
            high_pnl = np.mean([s["mean_pnl_pts"] for s in train_sessions if s[best_feature] >= threshold])
            low_pnl = np.mean([s["mean_pnl_pts"] for s in train_sessions if s[best_feature] < threshold])
            trade_high = high_pnl >= low_pnl

            # Apply to test
            if trade_high and test_session[best_feature] >= threshold:
                loo_filtered_trades.extend(test_session["trades"].tolist())
            elif not trade_high and test_session[best_feature] < threshold:
                loo_filtered_trades.extend(test_session["trades"].tolist())

        loo_all_trades.extend(test_session["trades"].tolist())

    loo_all = np.array(loo_all_trades)
    loo_filt = np.array(loo_filtered_trades) if loo_filtered_trades else np.array([0.0])

    base_loo_sharpe = loo_all.mean() / loo_all.std() * np.sqrt(252 * 10) if loo_all.std() > 0 else 0
    filt_loo_sharpe = loo_filt.mean() / loo_filt.std() * np.sqrt(252 * 10) if len(loo_filt) > 1 and loo_filt.std() > 0 else 0

    print(f"  Best feature for regime: {best_feature}")
    print(f"  Base (all days) OOS Sharpe: {base_loo_sharpe:.2f} ({len(loo_all)} trades)")
    print(f"  Filtered OOS Sharpe: {filt_loo_sharpe:.2f} ({len(loo_filt)} trades)")
    print(f"  OOS Sharpe improvement: {filt_loo_sharpe - base_loo_sharpe:+.2f}")

    # Kill gate
    print(f"\n{'='*80}")
    print(f"KILL GATE VERDICT")
    print(f"{'='*80}")

    oos_improvement = filt_loo_sharpe - base_loo_sharpe
    print(f"  OOS Sharpe improvement: {oos_improvement:+.2f} (threshold: ≥0.3)")

    if oos_improvement >= 0.3:
        print("  VERDICT: PASS — regime filter adds meaningful OOS value")
    else:
        print("  VERDICT: FAIL — regime filter does NOT improve OOS Sharpe ≥ 0.3")

    # Also report: is the base signal even profitable?
    print(f"\n  NOTE: Base signal mean P&L = {base_mean:+.2f} pts/trade")
    if base_mean < 0:
        print("  WARNING: Base signal is NEGATIVE — filtering a losing signal is futile")
    elif base_mean < 8:  # less than round-trip cost
        print(f"  WARNING: Base signal mean ({base_mean:.1f}) < round-trip cost (7-8 pts)")


if __name__ == "__main__":
    main()
