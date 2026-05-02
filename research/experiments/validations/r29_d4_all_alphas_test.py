"""R29 D4 × All Alphas: Systematic Regime Filter Test

Tests D4 opening regime filter (open_ofi_abs, open_abs_ret, open_volatility)
against every viable local alpha signal.

For each alpha:
1. Feed tick data through alpha.update() to generate signals
2. Trade on signal direction, hold 30 min
3. Split by D4 regime features (high/low opening OFI etc.)
4. Report filtered vs unfiltered Sharpe and mean P&L

Walk-forward: LOO cross-validation.
"""

import os
import sys
import math
import importlib
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

GOLDEN_DIR = Path("research/data/real/golden")
TX_SYMBOLS = ["TXFD6", "TXFB6", "TXFC6", "TXFE6"]

# Hold period in 1s bars after signal
HOLD_BARS = 1800  # 30 min
# Signal sampling interval (seconds)
SAMPLE_INTERVAL = 300  # sample signal every 5 min
# Opening period for regime features
OPEN_PERIOD_S = 1800  # 30 min
# Cost in points
COST_PTS = 8.0


# ── Alpha Adapters ────────────────────────────────────────────────────────

def _make_adapter(alpha_name: str):
    """Create (alpha_instance, feed_func) for a given alpha.

    feed_func(alpha, row) feeds one BidAsk tick to the alpha and
    returns the signal value (float) or None if not warmed up.
    """
    mod = importlib.import_module(f"research.alphas.{alpha_name}.impl")

    if alpha_name == "regime_adaptive_ofi":
        cls = mod.RegimeAdaptiveOFI
        inst = cls()

        def feed(a, bid_px, ask_px, bid_qty, ask_qty, mid, spread):
            r = a.update(bid_px, ask_px, bid_qty, ask_qty, mid, spread)
            return r.get("signal", 0) if isinstance(r, dict) else r

        return inst, feed

    if alpha_name == "log_gofi":
        cls = mod.LogGofiAlpha
        inst = cls()

        def feed(a, bid_px, ask_px, bid_qty, ask_qty, mid, spread):
            r = a.update(bid_px=bid_px, ask_px=ask_px, bid_qty=bid_qty, ask_qty=ask_qty)
            return r.get("signal", 0) if isinstance(r, dict) else r

        return inst, feed

    if alpha_name == "critical_trend_reversion":
        cls = mod.CriticalTrendReversion
        inst = cls()

        def feed(a, bid_px, ask_px, bid_qty, ask_qty, mid, spread):
            mid_x2 = int(bid_px + ask_px)  # mid_x2 = bid + ask (already scaled)
            r = a.update(mid_x2)
            return r.get("signal", 0) if isinstance(r, dict) else r

        return inst, feed

    if alpha_name == "intraday_jump_recovery":
        cls = mod.IntradayJumpRecovery
        inst = cls()

        def feed(a, bid_px, ask_px, bid_qty, ask_qty, mid, spread):
            mid_x2 = int(bid_px + ask_px)
            r = a.update(mid_x2)
            return r.get("signal", 0) if isinstance(r, dict) else r

        return inst, feed

    if alpha_name == "multiscale_trend_reversion":
        cls = mod.MultiscaleTrendReversion
        inst = cls()

        def feed(a, bid_px, ask_px, bid_qty, ask_qty, mid, spread):
            r = a.update(float(mid))
            return r.get("signal", 0) if isinstance(r, dict) else r

        return inst, feed

    if alpha_name == "vol_cbs":
        cls = mod.VolCBS
        inst = cls()

        def feed(a, bid_px, ask_px, bid_qty, ask_qty, mid, spread):
            mid_x2 = int(bid_px + ask_px)
            r = a.update(mid_x2)
            return r.get("signal", 0) if isinstance(r, dict) else r

        return inst, feed

    if alpha_name == "vr_momentum":
        cls = mod.VRMomentum
        inst = cls()

        def feed(a, bid_px, ask_px, bid_qty, ask_qty, mid, spread):
            mid_x2 = int(bid_px + ask_px)
            r = a.update(mid_x2)
            return r.get("signal", 0) if isinstance(r, dict) else r

        return inst, feed

    if alpha_name == "intensity_burst":
        cls = mod.IntensityBurstAlpha
        inst = cls()

        def feed(a, bid_px, ask_px, bid_qty, ask_qty, mid, spread):
            import time
            r = a.update(int(time.time_ns()))
            return r.get("signal", 0) if isinstance(r, dict) else r

        return inst, feed

    return None, None


# List of alphas to test (those with clean interfaces)
ALPHA_NAMES = [
    "regime_adaptive_ofi",
    "log_gofi",
    "critical_trend_reversion",
    "intraday_jump_recovery",
    "multiscale_trend_reversion",
    "vol_cbs",
    "vr_momentum",
]


def load_bidask(parquet_path: str) -> pd.DataFrame:
    """Load L5 BidAsk data."""
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

    ba["hour"] = pd.to_datetime(ba["ts_s"], unit="s").dt.hour
    ba = ba[(ba["hour"] >= 0) & (ba["hour"] <= 6)]

    return ba[["ts_s", "best_bid_px", "best_ask_px", "best_bid_qty",
               "best_ask_qty", "mid", "spread"]].reset_index(drop=True)


def compute_ofi_array(ba: pd.DataFrame) -> np.ndarray:
    """Compute per-tick OFI (for opening features)."""
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


def run_alpha_on_day(alpha_name: str, ba: pd.DataFrame) -> dict | None:
    """Run one alpha on one day's data.

    Returns dict with opening features and trade-level P&L.
    """
    inst, feed = _make_adapter(alpha_name)
    if inst is None:
        return None

    # Reset alpha
    inst.reset()

    ts_s = ba["ts_s"].values
    mids = ba["mid"].values
    start_ts = ts_s[0]
    open_end = start_ts + OPEN_PERIOD_S

    # Compute OFI for opening features
    ofi = compute_ofi_array(ba)

    # Opening features
    open_mask = ts_s < open_end
    open_ofi_cum = ofi[open_mask].sum()
    open_ofi_abs = abs(open_ofi_cum)
    open_mid_start = mids[open_mask][0] if open_mask.any() else 0
    open_mid_end = mids[open_mask][-1] if open_mask.any() else 0
    open_abs_ret = abs((open_mid_end - open_mid_start) / open_mid_start) if open_mid_start > 0 else 0

    # Opening volatility (5-min bar returns)
    open_bars = ba[open_mask].copy()
    open_bars["ts_5min"] = (np.floor(open_bars["ts_s"].values) // 300 * 300).astype(int)
    ob5 = open_bars.groupby("ts_5min").agg(mid=("mid", "last")).reset_index()
    if len(ob5) >= 3:
        open_vol = np.std(np.diff(ob5["mid"].values) / ob5["mid"].values[:-1])
    else:
        open_vol = 0.0

    # Resample to 1s bars for signal sampling
    ba_copy = ba.copy()
    ba_copy["ts_floor"] = np.floor(ba_copy["ts_s"]).astype(int)

    # Feed ALL ticks through alpha (including opening)
    signals_at_1s = {}
    for idx in range(len(ba)):
        r = feed(
            inst,
            ba["best_bid_px"].iloc[idx],
            ba["best_ask_px"].iloc[idx],
            ba["best_bid_qty"].iloc[idx],
            ba["best_ask_qty"].iloc[idx],
            ba["mid"].iloc[idx],
            ba["spread"].iloc[idx] / ba["mid"].iloc[idx] * 10000 if ba["mid"].iloc[idx] > 0 else 0,  # spread_bps
        )
        ts_1s = int(ts_s[idx])
        if r is not None and r != 0:
            signals_at_1s[ts_1s] = (r, mids[idx])

    if not signals_at_1s:
        return None

    # Create 1s bar mid prices for forward return calculation
    bars_1s = ba_copy.groupby("ts_floor").agg(mid=("mid", "last")).reset_index()
    mid_map = dict(zip(bars_1s["ts_floor"].values, bars_1s["mid"].values))

    # Sample signals every SAMPLE_INTERVAL seconds, after opening period
    rest_start = int(open_end)
    rest_end = int(ts_s[-1]) - HOLD_BARS

    trades = []
    t = rest_start
    while t <= rest_end:
        # Find latest signal at or before t
        sig_val = None
        sig_mid = None
        for dt in range(0, SAMPLE_INTERVAL):
            if (t - dt) in signals_at_1s:
                sig_val, sig_mid = signals_at_1s[t - dt]
                break

        if sig_val is not None and sig_val != 0:
            direction = 1.0 if sig_val > 0 else -1.0
            entry_mid = mid_map.get(t, sig_mid)

            # Forward exit at t + HOLD_BARS
            exit_mid = mid_map.get(t + HOLD_BARS)
            if exit_mid is None:
                # Find closest available
                for dt2 in range(-30, 31):
                    if (t + HOLD_BARS + dt2) in mid_map:
                        exit_mid = mid_map[t + HOLD_BARS + dt2]
                        break

            if entry_mid is not None and exit_mid is not None and entry_mid > 0:
                pnl_pts = direction * (exit_mid - entry_mid) / 1e6
                trades.append(pnl_pts)

        t += SAMPLE_INTERVAL

    if not trades:
        return None

    trades_arr = np.array(trades)

    return {
        "open_ofi_abs": open_ofi_abs,
        "open_abs_ret": open_abs_ret,
        "open_volatility": open_vol,
        "n_trades": len(trades_arr),
        "mean_pnl": trades_arr.mean(),
        "total_pnl": trades_arr.sum(),
        "win_rate": (trades_arr > 0).sum() / len(trades_arr),
        "trades": trades_arr,
    }


def compute_sharpe(trades: np.ndarray, trades_per_day: float = 10.0) -> float:
    """Annualized Sharpe from trade P&L array."""
    if len(trades) < 2 or trades.std() < 1e-10:
        return 0.0
    return trades.mean() / trades.std() * math.sqrt(252 * trades_per_day)


def main():
    # Load all available data
    print("Loading data...")
    day_data = {}
    for sym in TX_SYMBOLS:
        sym_dir = GOLDEN_DIR / sym
        if not sym_dir.exists():
            continue
        for pq in sorted(sym_dir.glob("*.parquet")):
            ba = load_bidask(str(pq))
            if len(ba) >= 5000:
                key = f"{sym}_{pq.stem}"
                day_data[key] = ba
                print(f"  {key}: {len(ba)} events")

    print(f"\nLoaded {len(day_data)} trading days\n")

    # Test each alpha
    all_results = []

    for alpha_name in ALPHA_NAMES:
        print(f"\n{'='*70}")
        print(f"ALPHA: {alpha_name}")
        print(f"{'='*70}")

        sessions = []
        for day_key, ba in day_data.items():
            try:
                result = run_alpha_on_day(alpha_name, ba)
                if result is not None and result["n_trades"] >= 3:
                    result["day"] = day_key
                    sessions.append(result)
                    print(f"  {day_key}: {result['n_trades']} trades, "
                          f"mean={result['mean_pnl']:+.1f} pts, wr={result['win_rate']:.0%}")
                else:
                    print(f"  {day_key}: no trades")
            except Exception as e:
                print(f"  {day_key}: ERROR - {e}")

        if len(sessions) < 3:
            print(f"  SKIP — only {len(sessions)} usable sessions")
            all_results.append({
                "alpha": alpha_name, "status": "SKIP",
                "base_sharpe": 0, "best_filtered_sharpe": 0,
                "best_feature": "", "improvement": 0,
            })
            continue

        # Base signal performance
        all_trades = np.concatenate([s["trades"] for s in sessions])
        base_sharpe = compute_sharpe(all_trades)
        base_mean = all_trades.mean()
        base_wr = (all_trades > 0).sum() / len(all_trades)

        print(f"\n  BASE: {len(all_trades)} trades, mean={base_mean:+.2f} pts, "
              f"wr={base_wr:.1%}, sharpe={base_sharpe:.2f}")

        # Test regime filters
        best_improvement = -999
        best_feature = ""
        best_sharpe = base_sharpe
        best_detail = ""

        sdf = pd.DataFrame([{k: v for k, v in s.items() if k != "trades"} for s in sessions])

        for feat in ["open_ofi_abs", "open_abs_ret", "open_volatility"]:
            med = sdf[feat].median()
            high_idx = sdf[feat] >= med
            low_idx = ~high_idx

            high_trades = np.concatenate([sessions[i]["trades"] for i in range(len(sessions)) if high_idx.iloc[i]])
            low_trades = np.concatenate([sessions[i]["trades"] for i in range(len(sessions)) if low_idx.iloc[i]])

            if len(high_trades) < 5 or len(low_trades) < 5:
                continue

            h_sharpe = compute_sharpe(high_trades)
            l_sharpe = compute_sharpe(low_trades)
            h_mean = high_trades.mean()
            l_mean = low_trades.mean()

            best_half = max(h_sharpe, l_sharpe)
            which = "HIGH" if h_sharpe >= l_sharpe else "LOW"
            impr = best_half - base_sharpe

            print(f"  {feat}: HIGH mean={h_mean:+.1f} sharpe={h_sharpe:.2f} | "
                  f"LOW mean={l_mean:+.1f} sharpe={l_sharpe:.2f} | "
                  f"best={which} impr={impr:+.2f}")

            if impr > best_improvement:
                best_improvement = impr
                best_feature = f"{feat}:{which}"
                best_sharpe = best_half
                best_mean = h_mean if which == "HIGH" else l_mean
                best_detail = f"mean={best_mean:+.1f}, n={len(high_trades) if which == 'HIGH' else len(low_trades)}"

        # LOO cross-validation for best feature
        if best_feature:
            feat_name, which_half = best_feature.split(":")
            loo_filtered = []
            loo_all = []

            for i in range(len(sessions)):
                train = [sessions[j] for j in range(len(sessions)) if j != i]
                test = sessions[i]

                train_vals = [s[feat_name] for s in train]
                threshold = np.median(train_vals)

                # Which half was better in training?
                train_high_pnl = np.mean([s["mean_pnl"] for s in train if s[feat_name] >= threshold])
                train_low_pnl = np.mean([s["mean_pnl"] for s in train if s[feat_name] < threshold])
                trade_high = train_high_pnl >= train_low_pnl

                if trade_high and test[feat_name] >= threshold:
                    loo_filtered.extend(test["trades"].tolist())
                elif not trade_high and test[feat_name] < threshold:
                    loo_filtered.extend(test["trades"].tolist())

                loo_all.extend(test["trades"].tolist())

            loo_all_arr = np.array(loo_all)
            loo_filt_arr = np.array(loo_filtered) if loo_filtered else np.array([0.0])

            loo_base = compute_sharpe(loo_all_arr)
            loo_filt = compute_sharpe(loo_filt_arr)
            loo_impr = loo_filt - loo_base
            filt_mean = loo_filt_arr.mean() if len(loo_filt_arr) > 0 else 0

            print(f"\n  LOO: base_sharpe={loo_base:.2f} -> filtered={loo_filt:.2f} "
                  f"(Δ={loo_impr:+.2f})")
            print(f"  LOO filtered: mean={filt_mean:+.1f} pts, n={len(loo_filt_arr)} trades")

            clears_cost = filt_mean > COST_PTS
            print(f"  Clears {COST_PTS} pts cost? {'YES' if clears_cost else 'NO'} "
                  f"({filt_mean:+.1f} vs {COST_PTS})")
        else:
            loo_filt = base_sharpe
            loo_impr = 0
            filt_mean = base_mean
            clears_cost = False

        all_results.append({
            "alpha": alpha_name,
            "status": "TESTED",
            "n_sessions": len(sessions),
            "n_trades": len(all_trades),
            "base_mean_pts": base_mean,
            "base_sharpe": base_sharpe,
            "best_feature": best_feature,
            "best_filtered_sharpe": best_sharpe,
            "improvement": best_improvement,
            "loo_base_sharpe": loo_base if best_feature else base_sharpe,
            "loo_filtered_sharpe": loo_filt,
            "loo_improvement": loo_impr if best_feature else 0,
            "filtered_mean_pts": filt_mean,
            "clears_cost": clears_cost,
        })

    # Final summary
    print(f"\n{'='*80}")
    print(f"FINAL SUMMARY: D4 Regime Filter × All Alphas")
    print(f"{'='*80}")
    print(f"{'Alpha':<30s} {'Base':>6s} {'Filt':>6s} {'ΔOOS':>6s} {'Mean':>7s} {'Cost?':>5s} {'Feature':<25s}")
    print("-" * 90)

    for r in sorted(all_results, key=lambda x: x.get("loo_improvement", 0), reverse=True):
        if r["status"] == "SKIP":
            print(f"{r['alpha']:<30s} {'SKIP':>6s}")
            continue

        print(f"{r['alpha']:<30s} "
              f"{r.get('loo_base_sharpe', 0):>6.2f} "
              f"{r.get('loo_filtered_sharpe', 0):>6.2f} "
              f"{r.get('loo_improvement', 0):>+6.2f} "
              f"{r.get('filtered_mean_pts', 0):>+7.1f} "
              f"{'YES' if r.get('clears_cost') else 'NO':>5s} "
              f"{r.get('best_feature', ''):25s}")

    # Highlight winners
    winners = [r for r in all_results if r.get("clears_cost")]
    print(f"\n  WINNERS (clears {COST_PTS} pts cost after D4 filter): {len(winners)}")
    for w in winners:
        print(f"    {w['alpha']}: filtered_mean={w['filtered_mean_pts']:+.1f} pts, "
              f"sharpe={w['loo_filtered_sharpe']:.2f}, filter={w['best_feature']}")


if __name__ == "__main__":
    main()
