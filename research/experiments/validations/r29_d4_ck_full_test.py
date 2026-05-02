"""R29 D4 × All Alphas — Full 32-day ClickHouse Test

Queries CK for front-month TX BidAsk L5 data per day,
runs D4 regime filter × all viable alphas.
"""

import os
import sys
import math
import importlib
import subprocess
import json
import numpy as np
import pandas as pd
from io import StringIO
from pathlib import Path
from scipy import stats

# ── Config ────────────────────────────────────────────────────────────────

HOLD_BARS = 1800      # 30 min hold
SAMPLE_INTERVAL = 300  # signal every 5 min
OPEN_PERIOD_S = 1800   # 30 min opening
COST_PTS = 8.0

# Front-month mapping (from CK analysis)
FRONT_MONTH = {
    "2026-01-26": "TXFB6", "2026-01-27": "TXFB6", "2026-01-28": "TXFB6",
    "2026-01-29": "TXFB6", "2026-01-30": "TXFB6", "2026-01-31": "TXFB6",
    "2026-02-03": "TXFB6", "2026-02-04": "TXFB6", "2026-02-05": "TXFB6",
    "2026-02-06": "TXFB6", "2026-02-23": "TXFD6", "2026-02-24": "TXFD6",
    "2026-02-25": "TXFD6", "2026-02-26": "TXFC6", "2026-03-03": "TXFC6",
    "2026-03-04": "TXFC6", "2026-03-05": "TXFC6", "2026-03-06": "TXFC6",
    "2026-03-09": "TXFC6", "2026-03-10": "TXFC6", "2026-03-11": "TXFC6",
    "2026-03-12": "TXFC6", "2026-03-13": "TXFC6", "2026-03-16": "TXFC6",
    "2026-03-17": "TXFC6", "2026-03-18": "TXFE6", "2026-03-19": "TXFD6",
    "2026-03-20": "TXFD6", "2026-03-23": "TXFD6", "2026-03-24": "TXFD6",
    "2026-03-26": "TXFD6", "2026-03-27": "TXFD6",
}

ALPHA_NAMES = [
    "regime_adaptive_ofi",
    "log_gofi",
    "critical_trend_reversion",
    "intraday_jump_recovery",
    "multiscale_trend_reversion",
    "vol_cbs",
    "vr_momentum",
]


# ── CK Data Loading ──────────────────────────────────────────────────────

def query_ck(sql: str) -> str:
    """Run a ClickHouse query via docker exec."""
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client",
         "--query", sql, "--format", "TabSeparatedWithNames"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CK error: {result.stderr[:500]}")
    return result.stdout


def load_day_from_ck(date_str: str, symbol: str) -> pd.DataFrame:
    """Load one day's BidAsk L5 from ClickHouse."""
    sql = f"""
    SELECT
        exch_ts,
        bids_price[1] as best_bid_px,
        asks_price[1] as best_ask_px,
        bids_vol[1] as best_bid_qty,
        asks_vol[1] as best_ask_qty
    FROM hft.market_data
    WHERE symbol = '{symbol}'
      AND type = 'BidAsk'
      AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
      AND bids_price[1] > 0
      AND asks_price[1] > 0
      AND asks_price[1] > bids_price[1]
    ORDER BY exch_ts
    """
    raw = query_ck(sql)
    if not raw.strip():
        return pd.DataFrame()

    df = pd.read_csv(StringIO(raw), sep="\t")
    if len(df) < 100:
        return pd.DataFrame()

    df["mid"] = (df["best_bid_px"] + df["best_ask_px"]) / 2.0
    df["spread"] = df["best_ask_px"] - df["best_bid_px"]
    df["ts_s"] = df["exch_ts"].astype(float) / 1e9

    # Filter to regular session hours (TAIFEX 08:45-13:45 CST = 00:45-05:45 UTC)
    df["hour"] = pd.to_datetime(df["ts_s"], unit="s").dt.hour
    df = df[(df["hour"] >= 0) & (df["hour"] <= 6)]

    return df[["ts_s", "best_bid_px", "best_ask_px", "best_bid_qty",
               "best_ask_qty", "mid", "spread"]].reset_index(drop=True)


# ── OFI / Alpha / Signal Logic ───────────────────────────────────────────

def compute_ofi_vectorized(ba: pd.DataFrame) -> np.ndarray:
    """Vectorized per-tick OFI computation."""
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


def _make_adapter(alpha_name: str):
    """Create alpha instance + feed function."""
    mod = importlib.import_module(f"research.alphas.{alpha_name}.impl")

    if alpha_name == "regime_adaptive_ofi":
        inst = mod.RegimeAdaptiveOFI()
        def feed(a, bp, ap, bq, aq, mid, sp_bps):
            r = a.update(bp, ap, bq, aq, mid, sp_bps)
            return r.get("signal", 0) if isinstance(r, dict) else r
        return inst, feed

    if alpha_name == "log_gofi":
        inst = mod.LogGofiAlpha()
        def feed(a, bp, ap, bq, aq, mid, sp_bps):
            r = a.update(bid_px=bp, ask_px=ap, bid_qty=bq, ask_qty=aq)
            return r.get("signal", 0) if isinstance(r, dict) else r
        return inst, feed

    if alpha_name in ("critical_trend_reversion", "intraday_jump_recovery",
                       "vol_cbs", "vr_momentum"):
        cls_map = {
            "critical_trend_reversion": "CriticalTrendReversion",
            "intraday_jump_recovery": "IntradayJumpRecovery",
            "vol_cbs": "VolCBS",
            "vr_momentum": "VRMomentum",
        }
        cls = getattr(mod, cls_map[alpha_name])
        inst = cls()
        def feed(a, bp, ap, bq, aq, mid, sp_bps):
            mid_x2 = int(bp + ap)
            r = a.update(mid_x2)
            return r.get("signal", 0) if isinstance(r, dict) else r
        return inst, feed

    if alpha_name == "multiscale_trend_reversion":
        inst = mod.MultiscaleTrendReversion()
        def feed(a, bp, ap, bq, aq, mid, sp_bps):
            r = a.update(float(mid))
            return r.get("signal", 0) if isinstance(r, dict) else r
        return inst, feed

    return None, None


def run_alpha_on_day(alpha_name: str, ba: pd.DataFrame) -> dict | None:
    """Run alpha on one day. Returns opening features + trades."""
    inst, feed = _make_adapter(alpha_name)
    if inst is None:
        return None

    inst.reset()

    ts_s = ba["ts_s"].values
    mids = ba["mid"].values
    start_ts = ts_s[0]
    open_end = start_ts + OPEN_PERIOD_S

    # Opening features from OFI
    ofi = compute_ofi_vectorized(ba)
    open_mask = ts_s < open_end
    open_ofi_cum = ofi[open_mask].sum()
    open_ofi_abs = abs(open_ofi_cum)

    open_mid_s = mids[open_mask][0] if open_mask.any() else 0
    open_mid_e = mids[open_mask][-1] if open_mask.any() else 0
    open_abs_ret = abs((open_mid_e - open_mid_s) / open_mid_s) if open_mid_s > 0 else 0

    # Opening volatility
    open_bars = ba[open_mask].copy()
    open_bars["ts_5min"] = (np.floor(open_bars["ts_s"].values) // 300 * 300).astype(int)
    ob5 = open_bars.groupby("ts_5min").agg(mid=("mid", "last")).reset_index()
    open_vol = np.std(np.diff(ob5["mid"].values) / ob5["mid"].values[:-1]) if len(ob5) >= 3 else 0.0

    # Feed all ticks, collect signals at 1s resolution
    # Subsample ticks for speed: keep every Nth tick to ~50k max
    n_ticks = len(ba)
    step = max(1, n_ticks // 80000)

    signals_at_1s = {}
    bp = ba["best_bid_px"].values
    ap = ba["best_ask_px"].values
    bq = ba["best_bid_qty"].values
    aq = ba["best_ask_qty"].values
    sp = ba["spread"].values

    for idx in range(0, n_ticks, step):
        mid_v = mids[idx]
        sp_bps = sp[idx] / mid_v * 10000 if mid_v > 0 else 0
        try:
            r = feed(inst, bp[idx], ap[idx], bq[idx], aq[idx], mid_v, sp_bps)
        except Exception:
            continue

        ts_1s = int(ts_s[idx])
        if r is not None and r != 0:
            signals_at_1s[ts_1s] = (r, mid_v)

    if not signals_at_1s:
        return None

    # 1s mid map for forward returns
    ba_c = ba.copy()
    ba_c["ts_floor"] = np.floor(ba_c["ts_s"]).astype(int)
    bars_1s = ba_c.groupby("ts_floor").agg(mid=("mid", "last")).reset_index()
    mid_map = dict(zip(bars_1s["ts_floor"].values, bars_1s["mid"].values))

    # Generate trades
    rest_start = int(open_end)
    rest_end = int(ts_s[-1]) - HOLD_BARS
    trades = []

    t = rest_start
    while t <= rest_end:
        sig_val = None
        sig_mid = None
        for dt in range(0, SAMPLE_INTERVAL):
            if (t - dt) in signals_at_1s:
                sig_val, sig_mid = signals_at_1s[t - dt]
                break

        if sig_val is not None and sig_val != 0:
            direction = 1.0 if sig_val > 0 else -1.0
            entry_mid = mid_map.get(t, sig_mid)

            exit_mid = None
            for dt2 in range(-30, 31):
                if (t + HOLD_BARS + dt2) in mid_map:
                    exit_mid = mid_map[t + HOLD_BARS + dt2]
                    break

            if entry_mid and exit_mid and entry_mid > 0:
                pnl_pts = direction * (exit_mid - entry_mid) / 1e6
                trades.append(pnl_pts)

        t += SAMPLE_INTERVAL

    if not trades:
        return None

    return {
        "open_ofi_abs": open_ofi_abs,
        "open_abs_ret": open_abs_ret,
        "open_volatility": open_vol,
        "n_trades": len(trades),
        "mean_pnl": np.mean(trades),
        "total_pnl": np.sum(trades),
        "win_rate": np.sum(np.array(trades) > 0) / len(trades),
        "trades": np.array(trades),
    }


def compute_sharpe(trades: np.ndarray, trades_per_day: float = 10.0) -> float:
    if len(trades) < 2 or np.std(trades) < 1e-10:
        return 0.0
    return np.mean(trades) / np.std(trades) * math.sqrt(252 * trades_per_day)


# ── Also test a simple OFI momentum signal (no alpha import needed) ──────

def run_ofi_momentum_on_day(ba: pd.DataFrame) -> dict | None:
    """Simple OFI momentum: sign of 5-min cumulative OFI → hold 30 min."""
    ts_s = ba["ts_s"].values
    mids = ba["mid"].values
    start_ts = ts_s[0]
    open_end = start_ts + OPEN_PERIOD_S

    ofi = compute_ofi_vectorized(ba)
    open_mask = ts_s < open_end
    open_ofi_cum = ofi[open_mask].sum()
    open_ofi_abs = abs(open_ofi_cum)

    open_mid_s = mids[open_mask][0] if open_mask.any() else 0
    open_mid_e = mids[open_mask][-1] if open_mask.any() else 0
    open_abs_ret = abs((open_mid_e - open_mid_s) / open_mid_s) if open_mid_s > 0 else 0

    open_bars = ba[open_mask].copy()
    open_bars["ts_5min"] = (np.floor(open_bars["ts_s"].values) // 300 * 300).astype(int)
    ob5 = open_bars.groupby("ts_5min").agg(mid=("mid", "last")).reset_index()
    open_vol = np.std(np.diff(ob5["mid"].values) / ob5["mid"].values[:-1]) if len(ob5) >= 3 else 0.0

    # 1s bars
    ba_c = ba.copy()
    ba_c["ofi"] = ofi
    ba_c["ts_floor"] = np.floor(ba_c["ts_s"]).astype(int)
    bars_1s = ba_c.groupby("ts_floor").agg(
        ofi_sum=("ofi", "sum"), mid_last=("mid", "last"),
    ).reset_index()
    mid_map = dict(zip(bars_1s["ts_floor"].values, bars_1s["mid_last"].values))
    ofi_map = dict(zip(bars_1s["ts_floor"].values, bars_1s["ofi_sum"].values))

    rest_start = int(open_end)
    rest_end = int(ts_s[-1]) - HOLD_BARS
    trades = []

    t = rest_start
    while t <= rest_end:
        # 5-min cumulative OFI
        cum_ofi = sum(ofi_map.get(t - dt, 0) for dt in range(SAMPLE_INTERVAL))
        if abs(cum_ofi) < 1:
            t += SAMPLE_INTERVAL
            continue

        direction = 1.0 if cum_ofi > 0 else -1.0
        entry_mid = mid_map.get(t)
        exit_mid = None
        for dt2 in range(-30, 31):
            if (t + HOLD_BARS + dt2) in mid_map:
                exit_mid = mid_map[t + HOLD_BARS + dt2]
                break

        if entry_mid and exit_mid and entry_mid > 0:
            pnl_pts = direction * (exit_mid - entry_mid) / 1e6
            trades.append(pnl_pts)

        t += SAMPLE_INTERVAL

    if not trades:
        return None

    return {
        "open_ofi_abs": open_ofi_abs,
        "open_abs_ret": open_abs_ret,
        "open_volatility": open_vol,
        "n_trades": len(trades),
        "mean_pnl": np.mean(trades),
        "total_pnl": np.sum(trades),
        "win_rate": np.sum(np.array(trades) > 0) / len(trades),
        "trades": np.array(trades),
    }


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    # Load all 32 days from CK
    print("Loading 32 days from ClickHouse (front-month TXF)...")
    day_data = {}
    for date_str, symbol in sorted(FRONT_MONTH.items()):
        print(f"  {date_str} ({symbol})...", end=" ", flush=True)
        try:
            ba = load_day_from_ck(date_str, symbol)
            if len(ba) >= 5000:
                day_data[date_str] = ba
                print(f"{len(ba)} events")
            else:
                print(f"SKIP ({len(ba)} events)")
        except Exception as e:
            print(f"ERROR: {e}")

    print(f"\nLoaded {len(day_data)} trading days\n")

    # Test all alphas + OFI momentum baseline
    all_alpha_names = ["_ofi_momentum"] + ALPHA_NAMES
    all_results = []

    for alpha_name in all_alpha_names:
        print(f"\n{'='*70}")
        print(f"ALPHA: {alpha_name}")
        print(f"{'='*70}")

        sessions = []
        for date_str, ba in sorted(day_data.items()):
            try:
                if alpha_name == "_ofi_momentum":
                    result = run_ofi_momentum_on_day(ba)
                else:
                    result = run_alpha_on_day(alpha_name, ba)

                if result is not None and result["n_trades"] >= 3:
                    result["day"] = date_str
                    sessions.append(result)
                    print(f"  {date_str}: {result['n_trades']} trades, "
                          f"mean={result['mean_pnl']:+.1f} pts, wr={result['win_rate']:.0%}")
                else:
                    print(f"  {date_str}: no trades")
            except Exception as e:
                print(f"  {date_str}: ERROR - {str(e)[:80]}")

        if len(sessions) < 5:
            print(f"  SKIP — only {len(sessions)} sessions")
            all_results.append({"alpha": alpha_name, "status": "SKIP"})
            continue

        all_trades = np.concatenate([s["trades"] for s in sessions])
        base_sharpe = compute_sharpe(all_trades)
        base_mean = all_trades.mean()
        base_wr = (all_trades > 0).sum() / len(all_trades)

        print(f"\n  BASE: {len(all_trades)} trades, {len(sessions)} days, "
              f"mean={base_mean:+.2f} pts, wr={base_wr:.1%}, sharpe={base_sharpe:.2f}")

        # Regime filter analysis
        sdf = pd.DataFrame([{k: v for k, v in s.items() if k != "trades"} for s in sessions])

        best_improvement = -999
        best_feature = ""
        best_sharpe = base_sharpe
        best_filt_mean = base_mean
        best_filt_n = len(all_trades)

        for feat in ["open_ofi_abs", "open_abs_ret", "open_volatility"]:
            med = sdf[feat].median()
            high_idx = sdf[feat] >= med
            low_idx = ~high_idx

            high_t = np.concatenate([sessions[i]["trades"] for i in range(len(sessions)) if high_idx.iloc[i]])
            low_t = np.concatenate([sessions[i]["trades"] for i in range(len(sessions)) if low_idx.iloc[i]])

            if len(high_t) < 10 or len(low_t) < 10:
                continue

            h_s = compute_sharpe(high_t)
            l_s = compute_sharpe(low_t)

            best_half = max(h_s, l_s)
            which = "HIGH" if h_s >= l_s else "LOW"
            impr = best_half - base_sharpe

            print(f"  {feat}: HIGH mean={high_t.mean():+.1f} sharpe={h_s:.2f} ({len(high_t)}) | "
                  f"LOW mean={low_t.mean():+.1f} sharpe={l_s:.2f} ({len(low_t)}) | best={which} Δ={impr:+.2f}")

            if impr > best_improvement:
                best_improvement = impr
                best_feature = f"{feat}:{which}"
                best_sharpe = best_half
                best_filt_mean = high_t.mean() if which == "HIGH" else low_t.mean()
                best_filt_n = len(high_t) if which == "HIGH" else len(low_t)

        # LOO cross-validation
        loo_base_sharpe = base_sharpe
        loo_filt_sharpe = base_sharpe
        loo_filt_mean = base_mean
        loo_filt_n = 0

        if best_feature:
            feat_name, which_half = best_feature.split(":")
            loo_filtered = []
            loo_all = []

            for i in range(len(sessions)):
                train = [sessions[j] for j in range(len(sessions)) if j != i]
                test = sessions[i]

                threshold = np.median([s[feat_name] for s in train])
                train_high_pnl = np.mean([s["mean_pnl"] for s in train if s[feat_name] >= threshold] or [0])
                train_low_pnl = np.mean([s["mean_pnl"] for s in train if s[feat_name] < threshold] or [0])
                trade_high = train_high_pnl >= train_low_pnl

                if trade_high and test[feat_name] >= threshold:
                    loo_filtered.extend(test["trades"].tolist())
                elif not trade_high and test[feat_name] < threshold:
                    loo_filtered.extend(test["trades"].tolist())

                loo_all.extend(test["trades"].tolist())

            loo_all_arr = np.array(loo_all)
            loo_filt_arr = np.array(loo_filtered) if loo_filtered else np.array([0.0])

            loo_base_sharpe = compute_sharpe(loo_all_arr)
            loo_filt_sharpe = compute_sharpe(loo_filt_arr)
            loo_filt_mean = loo_filt_arr.mean() if len(loo_filt_arr) > 0 else 0
            loo_filt_n = len(loo_filt_arr)

            print(f"\n  LOO CV: base={loo_base_sharpe:.2f} -> filtered={loo_filt_sharpe:.2f} "
                  f"(Δ={loo_filt_sharpe - loo_base_sharpe:+.2f})")
            print(f"  LOO filtered: mean={loo_filt_mean:+.1f} pts, n={loo_filt_n}, "
                  f"clears_cost={'YES' if loo_filt_mean > COST_PTS else 'NO'}")

        all_results.append({
            "alpha": alpha_name,
            "status": "TESTED",
            "n_sessions": len(sessions),
            "n_trades": len(all_trades),
            "base_mean": base_mean,
            "base_wr": base_wr,
            "base_sharpe": loo_base_sharpe,
            "best_feature": best_feature,
            "filt_sharpe": loo_filt_sharpe,
            "filt_mean": loo_filt_mean,
            "filt_n": loo_filt_n,
            "loo_delta": loo_filt_sharpe - loo_base_sharpe,
            "clears_cost": loo_filt_mean > COST_PTS,
        })

    # Final summary
    print(f"\n{'='*90}")
    print(f"FINAL SUMMARY: D4 × All Alphas (32 days from CK)")
    print(f"{'='*90}")
    print(f"{'Alpha':<30s} {'Days':>4s} {'Trd':>5s} {'BaseMn':>7s} {'BaseS':>6s} "
          f"{'FiltMn':>7s} {'FiltS':>6s} {'ΔOOS':>6s} {'Cost?':>5s} {'Feature':<25s}")
    print("-" * 105)

    for r in sorted(all_results, key=lambda x: x.get("loo_delta", -99), reverse=True):
        if r["status"] == "SKIP":
            print(f"{r['alpha']:<30s} SKIP")
            continue
        print(f"{r['alpha']:<30s} {r['n_sessions']:>4d} {r['n_trades']:>5d} "
              f"{r['base_mean']:>+7.1f} {r['base_sharpe']:>6.2f} "
              f"{r['filt_mean']:>+7.1f} {r['filt_sharpe']:>6.2f} "
              f"{r['loo_delta']:>+6.2f} "
              f"{'YES' if r['clears_cost'] else 'NO':>5s} {r.get('best_feature', ''):25s}")

    winners = [r for r in all_results if r.get("clears_cost")]
    print(f"\n  WINNERS (mean > {COST_PTS} pts after D4 LOO filter): {len(winners)}")
    for w in winners:
        print(f"    {w['alpha']}: filt_mean={w['filt_mean']:+.1f}, "
              f"sharpe={w['filt_sharpe']:.2f}, n={w['filt_n']}, filter={w['best_feature']}")


if __name__ == "__main__":
    main()
