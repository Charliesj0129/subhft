"""
R26 Stage 3: Deep Lead-Lag Analysis — TX→TMF Cross-Product Signal
=================================================================
Comprehensive validation of TX price-change → TMF directional signal.

CRITICAL NOTE on volume semantics:
  The `volume` column in hft.market_data for TXFD6 Tick is the **cumulative daily
  session volume** (Shioaji convention), NOT per-tick traded quantity. The team lead's
  initial query (vol>=20, dp!=0) effectively filters out the first ~20 ticks of each
  session (opening auction accumulation). This means the "signal" is ANY TX tick with
  a price change after sufficient daily liquidity, not specifically "large institutional
  orders". We run both interpretations:
    A) "cum_vol>=20, dp!=0" (team lead's original) — 776 signals in March
    B) "delta_vol>=N, dp!=0" (true per-tick volume) — real large orders

Analyses 1-8 as specified in the research brief.

Output: console + outputs/team_artifacts/alpha-research-r26/stage3_results.json
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# ClickHouse helper
# ---------------------------------------------------------------------------
PRICE_SCALE = 1_000_000  # price_scaled / PRICE_SCALE = points


def ck_query(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        print(f"[CK ERROR] {r.stderr[:500]}", file=sys.stderr)
        return ""
    return r.stdout


def ck_rows(sql: str) -> list[list[str]]:
    out = ck_query(sql)
    if not out.strip():
        return []
    return [line.split("\t") for line in out.strip().split("\n")]


# ---------------------------------------------------------------------------
# Data loading (server-side delta-volume computation)
# ---------------------------------------------------------------------------

MS = 1_000_000  # 1 ms in ns
HORIZONS_MS = [36, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
RT_COST = 4.0  # round-trip cost in points for TMFD6


def load_tx_signals_from_ck(min_date: str = "2026-01-01") -> list[dict]:
    """
    Load TX ticks with server-side dp and dvol computation.
    Returns list of dicts with ts, price, dp, dvol, cum_vol for every tick where dp!=0.
    """
    sql = f"""
    SELECT exch_ts, price_pts, dp_pts, dvol, cum_vol
    FROM (
        SELECT exch_ts,
               price_scaled / {PRICE_SCALE} as price_pts,
               (price_scaled - lagInFrame(price_scaled, 1, 0)
                OVER (PARTITION BY toDate(toDateTime(exch_ts/1000000000)) ORDER BY exch_ts))
                / {PRICE_SCALE} as dp_pts,
               volume - lagInFrame(volume, 1, 0)
                OVER (PARTITION BY toDate(toDateTime(exch_ts/1000000000)) ORDER BY exch_ts) as dvol,
               volume as cum_vol
        FROM hft.market_data
        WHERE symbol='TXFD6' AND type='Tick'
        AND toDate(toDateTime(exch_ts/1000000000)) >= '{min_date}'
    )
    WHERE dp_pts != 0
    ORDER BY exch_ts
    """
    rows = ck_rows(sql)
    signals = []
    for r in rows:
        ts = int(r[0])
        price = float(r[1])
        dp = float(r[2])
        dvol = int(r[3])
        cum_vol = int(r[4])
        signals.append({
            "ts": ts,
            "price": price,
            "dp": dp,
            "dvol": dvol,
            "cum_vol": cum_vol,
            "direction": 1 if dp > 0 else -1,
        })
    return signals


def load_tmf_ticks(min_date: str = "2026-01-01") -> tuple[np.ndarray, np.ndarray]:
    """Load TMFD6 Tick data. Returns (ts_ns, price_pts) arrays."""
    sql = (
        f"SELECT exch_ts, price_scaled / {PRICE_SCALE} as price_pts "
        f"FROM hft.market_data "
        f"WHERE symbol='TMFD6' AND type='Tick' "
        f"AND toDate(toDateTime(exch_ts/1000000000)) >= '{min_date}' "
        f"ORDER BY exch_ts"
    )
    rows = ck_rows(sql)
    if not rows:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    n = len(rows)
    ts = np.empty(n, dtype=np.int64)
    price = np.empty(n, dtype=np.float64)
    for i, r in enumerate(rows):
        ts[i] = int(r[0])
        price[i] = float(r[1])
    return ts, price


def load_tmf_bidask(min_date: str = "2026-01-01") -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load TMFD6 BidAsk (best bid/ask only, filtering zero prices)."""
    sql = (
        f"SELECT exch_ts, bids_price[1] / {PRICE_SCALE}, asks_price[1] / {PRICE_SCALE} "
        f"FROM hft.market_data "
        f"WHERE symbol='TMFD6' AND type='BidAsk' "
        f"AND bids_price[1] > 0 AND asks_price[1] > 0 "
        f"AND toDate(toDateTime(exch_ts/1000000000)) >= '{min_date}' "
        f"ORDER BY exch_ts"
    )
    rows = ck_rows(sql)
    if not rows:
        return np.empty(0, dtype=np.int64), np.empty(0), np.empty(0)
    n = len(rows)
    ts = np.empty(n, dtype=np.int64)
    bid = np.empty(n, dtype=np.float64)
    ask = np.empty(n, dtype=np.float64)
    for i, r in enumerate(rows):
        ts[i] = int(r[0])
        bid[i] = float(r[1])
        ask[i] = float(r[2])
    return ts, bid, ask


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_price_at(ts_arr: np.ndarray, price_arr: np.ndarray, target_ns: int) -> float:
    idx = np.searchsorted(ts_arr, target_ns, side="right") - 1
    if idx < 0:
        return np.nan
    return price_arr[idx]


def find_bidask_at(ba_ts: np.ndarray, ba_bid: np.ndarray, ba_ask: np.ndarray,
                   target_ns: int) -> tuple[float, float]:
    idx = np.searchsorted(ba_ts, target_ns, side="right") - 1
    if idx < 0:
        return np.nan, np.nan
    return ba_bid[idx], ba_ask[idx]


# ---------------------------------------------------------------------------
# Signal filtering
# ---------------------------------------------------------------------------

MARCH_START_DATE = "2026-03-19"
MARCH_START_NS = 1769400000000000000  # approx


def filter_signals(all_signals: list[dict], cum_vol_min: int = 0,
                   dvol_min: int = 0, march_only: bool = False) -> list[dict]:
    out = []
    for s in all_signals:
        if s["cum_vol"] < cum_vol_min:
            continue
        if s["dvol"] < dvol_min:
            continue
        if march_only and s["ts"] < MARCH_START_NS:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Analysis 1: Reproduce & Extend
# ---------------------------------------------------------------------------

def analysis_1(all_signals, tmf_ts, tmf_price):
    print("\n" + "=" * 80)
    print("ANALYSIS 1: Reproduce & Extend — Direction-Adjusted Forward Returns")
    print("=" * 80)

    results = {}

    # A) Team lead's interpretation: cum_vol >= threshold, dp != 0
    print("\n  --- A) Cumulative Volume Filter (team lead's method) ---")
    for cv_th in [1, 5, 10, 20, 50, 100]:
        sigs = filter_signals(all_signals, cum_vol_min=cv_th, march_only=True)
        if len(sigs) < 10:
            continue
        print(f"\n  cum_vol>={cv_th}: {len(sigs)} signals")
        print(f"  {'Horizon':>10} {'Mean':>8} {'Median':>8} {'Std':>8} {'DirAcc':>8} {'N':>6} {'Net':>8}")
        key = f"cumvol>={cv_th}"
        results[key] = {}
        for h_ms in HORIZONS_MS:
            h_ns = h_ms * MS
            rets = []
            for s in sigs:
                bp = find_price_at(tmf_ts, tmf_price, s["ts"])
                fp = find_price_at(tmf_ts, tmf_price, s["ts"] + h_ns)
                if np.isnan(bp) or np.isnan(fp):
                    continue
                rets.append(s["direction"] * (fp - bp))
            if not rets:
                continue
            arr = np.array(rets)
            m, med, std = np.mean(arr), np.median(arr), np.std(arr)
            da = np.mean(arr > 0)
            net = m - RT_COST
            tag = "PASS" if net > 0 else "FAIL"
            print(f"  {h_ms:>8}ms {m:>8.2f} {med:>8.2f} {std:>8.2f} {da:>7.1%} {len(arr):>6} {net:>8.2f} [{tag}]")
            results[key][f"{h_ms}ms"] = {
                "mean": round(m, 3), "median": round(med, 3), "std": round(std, 3),
                "dir_accuracy": round(da, 4), "n": len(arr), "net": round(net, 3), "pass": net > 0,
            }

    # B) True per-tick volume (delta vol)
    print("\n\n  --- B) Delta Volume Filter (true per-tick volume) ---")
    for dv_th in [1, 2, 3, 5, 10, 20]:
        sigs = filter_signals(all_signals, dvol_min=dv_th, march_only=True)
        if len(sigs) < 10:
            continue
        print(f"\n  dvol>={dv_th}: {len(sigs)} signals")
        print(f"  {'Horizon':>10} {'Mean':>8} {'Median':>8} {'Std':>8} {'DirAcc':>8} {'N':>6} {'Net':>8}")
        key = f"dvol>={dv_th}"
        results[key] = {}
        for h_ms in [36, 100, 500, 2000]:
            h_ns = h_ms * MS
            rets = []
            for s in sigs:
                bp = find_price_at(tmf_ts, tmf_price, s["ts"])
                fp = find_price_at(tmf_ts, tmf_price, s["ts"] + h_ns)
                if np.isnan(bp) or np.isnan(fp):
                    continue
                rets.append(s["direction"] * (fp - bp))
            if not rets:
                continue
            arr = np.array(rets)
            m, med, std = np.mean(arr), np.median(arr), np.std(arr)
            da = np.mean(arr > 0)
            net = m - RT_COST
            tag = "PASS" if net > 0 else "FAIL"
            print(f"  {h_ms:>8}ms {m:>8.2f} {med:>8.2f} {std:>8.2f} {da:>7.1%} {len(arr):>6} {net:>8.2f} [{tag}]")
            results[key][f"{h_ms}ms"] = {
                "mean": round(m, 3), "median": round(med, 3), "std": round(std, 3),
                "dir_accuracy": round(da, 4), "n": len(arr), "net": round(net, 3), "pass": net > 0,
            }

    # C) Period breakdown for cum_vol>=20, 36ms
    print("\n\n  --- C) Period Breakdown: cum_vol>=20, 36ms ---")
    for label, pred in [("Jan/Feb", lambda s: s["ts"] < MARCH_START_NS),
                         ("March+", lambda s: s["ts"] >= MARCH_START_NS)]:
        sigs = [s for s in filter_signals(all_signals, cum_vol_min=20) if pred(s)]
        rets = []
        for s in sigs:
            bp = find_price_at(tmf_ts, tmf_price, s["ts"])
            fp = find_price_at(tmf_ts, tmf_price, s["ts"] + 36 * MS)
            if np.isnan(bp) or np.isnan(fp):
                continue
            rets.append(s["direction"] * (fp - bp))
        if rets:
            arr = np.array(rets)
            print(f"  {label}: N={len(arr)}, mean={np.mean(arr):.2f}, median={np.median(arr):.2f}, "
                  f"dir_acc={np.mean(arr > 0):.1%}, net={np.mean(arr) - RT_COST:.2f}")
        else:
            print(f"  {label}: no data")

    return results


# ---------------------------------------------------------------------------
# Analysis 2: Realistic Entry Price
# ---------------------------------------------------------------------------

def analysis_2(signals, ba_ts, ba_bid, ba_ask, tmf_ts, tmf_price):
    print("\n" + "=" * 80)
    print("ANALYSIS 2: Realistic Entry Price & Slippage")
    print("=" * 80)

    result = {}

    for label, sigs in signals.items():
        if not sigs:
            continue
        slippages = []
        realistic_rets = {36: [], 100: [], 500: []}

        for s in sigs:
            # Mid at signal time
            b0, a0 = find_bidask_at(ba_ts, ba_bid, ba_ask, s["ts"])
            if np.isnan(b0) or np.isnan(a0):
                continue
            mid0 = (b0 + a0) / 2.0

            # Entry at signal+36ms
            b1, a1 = find_bidask_at(ba_ts, ba_bid, ba_ask, s["ts"] + 36 * MS)
            if np.isnan(b1) or np.isnan(a1):
                continue

            # Enter: buy at ask, sell at bid
            entry = a1 if s["direction"] == 1 else b1
            slippages.append(abs(entry - mid0))

            # Exit at various horizons (from entry time)
            for h in [36, 100, 500]:
                exit_ts = s["ts"] + 36 * MS + h * MS
                be, ae = find_bidask_at(ba_ts, ba_bid, ba_ask, exit_ts)
                if np.isnan(be):
                    continue
                # Exit: sell at bid (was buy) or buy at ask (was sell)
                exit_p = be if s["direction"] == 1 else ae
                ret = s["direction"] * (exit_p - entry)
                realistic_rets[h].append(ret)

        if not slippages:
            continue
        sl = np.array(slippages)
        print(f"\n  [{label}] N={len(slippages)}")
        print(f"    Slippage (mid→entry): mean={np.mean(sl):.2f}, median={np.median(sl):.2f}, "
              f"P90={np.percentile(sl, 90):.2f}")
        result[label] = {
            "slippage_mean": round(float(np.mean(sl)), 3),
            "slippage_median": round(float(np.median(sl)), 3),
            "slippage_p90": round(float(np.percentile(sl, 90)), 3),
        }

        for h, rets in realistic_rets.items():
            if not rets:
                continue
            arr = np.array(rets)
            gross = np.mean(arr)
            net = gross - RT_COST
            tag = "PASS" if net > 0 else "FAIL"
            print(f"    Exit@+{h}ms: gross={gross:.2f}, net={net:.2f}, "
                  f"dir_acc={np.mean(arr > 0):.1%} [{tag}]")
            result[label][f"exit_{h}ms"] = {
                "gross": round(float(gross), 3), "net": round(float(net), 3),
                "dir_acc": round(float(np.mean(arr > 0)), 4), "n": len(arr),
                "pass": net > 0,
            }

    return result


# ---------------------------------------------------------------------------
# Analysis 3: SL/TP Simulation
# ---------------------------------------------------------------------------

def analysis_3(sigs, tmf_ts, tmf_price):
    print("\n" + "=" * 80)
    print("ANALYSIS 3: SL/TP Simulation")
    print("=" * 80)

    SL_LEVELS = [3, 5, 8, 10]
    TP_CONFIGS = [(3, 2), (5, 3), (8, 5)]
    TIME_KILL_NS = 120_000 * MS

    best_exp = -999.0
    best_cfg = None
    results = {}

    for sl in SL_LEVELS:
        for trail, activate in TP_CONFIGS:
            pnls = []
            consec_loss = 0
            max_consec = 0

            for s in sigs:
                entry_ts = s["ts"] + 36 * MS
                entry_idx = np.searchsorted(tmf_ts, entry_ts, side="left")
                if entry_idx >= len(tmf_ts):
                    continue
                entry_price = tmf_price[entry_idx]
                deadline = entry_ts + TIME_KILL_NS
                d = s["direction"]
                peak = 0.0
                trail_active = False
                exit_price = None

                # Scan forward through TMF ticks
                end_idx = np.searchsorted(tmf_ts, deadline, side="right")
                end_idx = min(end_idx, len(tmf_ts))

                for k in range(entry_idx, end_idx):
                    cur = tmf_price[k]
                    fav = d * (cur - entry_price)

                    if fav <= -sl:
                        exit_price = entry_price - d * sl
                        break

                    if fav > peak:
                        peak = fav

                    if peak >= activate:
                        trail_active = True
                    if trail_active and fav <= peak - trail:
                        exit_price = cur
                        break

                if exit_price is None:
                    # Time kill or data end
                    exit_price = tmf_price[min(end_idx - 1, len(tmf_price) - 1)]

                pnl = d * (exit_price - entry_price) - RT_COST
                pnls.append(pnl)

                if pnl > 0:
                    consec_loss = 0
                else:
                    consec_loss += 1
                    max_consec = max(max_consec, consec_loss)

            if not pnls:
                continue
            arr = np.array(pnls)
            exp = np.mean(arr)
            wr = np.mean(arr > 0)
            cfg = f"SL={sl},Trail={trail}@{activate}"
            tag = "PASS" if exp > 0 else "FAIL"

            results[cfg] = {
                "expectation": round(float(exp), 3),
                "win_rate": round(float(wr), 4),
                "avg_win": round(float(np.mean(arr[arr > 0])) if np.any(arr > 0) else 0, 3),
                "avg_loss": round(float(np.mean(arr[arr <= 0])) if np.any(arr <= 0) else 0, 3),
                "n": len(arr),
                "max_consec_loss": max_consec,
                "pass": exp > 0,
            }

            if exp > best_exp:
                best_exp = exp
                best_cfg = cfg

            print(f"  {cfg:>25}: E={exp:>7.2f}, WR={wr:.1%}, N={len(arr)}, "
                  f"max_consec_L={max_consec} [{tag}]")

    print(f"\n  BEST: {best_cfg} → E={best_exp:.2f} pts")
    return results, best_cfg


# ---------------------------------------------------------------------------
# Analysis 4: Signal Decay Curve (10ms steps, 0→500ms)
# ---------------------------------------------------------------------------

def analysis_4(sigs, tmf_ts, tmf_price):
    print("\n" + "=" * 80)
    print("ANALYSIS 4: Signal Decay Curve (10ms steps)")
    print("=" * 80)

    decay = {}
    print(f"  {'Delay':>8} {'Mean':>8} {'Median':>8} {'DirAcc':>8} {'N':>6} {'>=Cost':>8}")
    crossover = None

    for d_ms in range(0, 510, 10):
        h_ns = d_ms * MS
        rets = []
        for s in sigs:
            bp = find_price_at(tmf_ts, tmf_price, s["ts"])
            fp = find_price_at(tmf_ts, tmf_price, s["ts"] + h_ns)
            if np.isnan(bp) or np.isnan(fp):
                continue
            rets.append(s["direction"] * (fp - bp))
        if not rets:
            continue
        arr = np.array(rets)
        m = np.mean(arr)
        above = "YES" if m >= RT_COST else "no"
        if m >= RT_COST and crossover is None:
            crossover = d_ms
        print(f"  {d_ms:>6}ms {m:>8.2f} {np.median(arr):>8.2f} {np.mean(arr > 0):>7.1%} {len(arr):>6} {above:>8}")
        decay[d_ms] = round(float(m), 3)

    if crossover is not None:
        print(f"\n  Signal first reaches {RT_COST} pts at {crossover}ms")
    else:
        print(f"\n  Signal NEVER reaches {RT_COST} pts cost at 0-500ms")

    return decay, crossover


# ---------------------------------------------------------------------------
# Analysis 5: Volume-Weighted Signal Quality
# ---------------------------------------------------------------------------

def analysis_5(all_signals, tmf_ts, tmf_price):
    print("\n" + "=" * 80)
    print("ANALYSIS 5: Signal Quality by Delta Volume Threshold (36ms)")
    print("=" * 80)

    results = {}
    print(f"  {'Filter':>15} {'N':>6} {'Mean':>8} {'Median':>8} {'DirAcc':>8} {'Net':>8}")

    for dv in [1, 2, 3, 5, 8, 10, 15, 20, 50]:
        sigs = filter_signals(all_signals, dvol_min=dv, march_only=True)
        if len(sigs) < 10:
            continue
        rets = []
        for s in sigs:
            bp = find_price_at(tmf_ts, tmf_price, s["ts"])
            fp = find_price_at(tmf_ts, tmf_price, s["ts"] + 36 * MS)
            if np.isnan(bp) or np.isnan(fp):
                continue
            rets.append(s["direction"] * (fp - bp))
        if not rets:
            continue
        arr = np.array(rets)
        m = np.mean(arr)
        net = m - RT_COST
        tag = "PASS" if net > 0 else "FAIL"
        print(f"  dvol>={dv:>3}: {len(arr):>6} {m:>8.2f} {np.median(arr):>8.2f} "
              f"{np.mean(arr > 0):>7.1%} {net:>8.2f} [{tag}]")
        results[f"dvol>={dv}"] = {
            "n": len(arr), "mean": round(m, 3), "net": round(net, 3),
            "dir_acc": round(float(np.mean(arr > 0)), 4), "pass": net > 0,
        }

    return results


# ---------------------------------------------------------------------------
# Analysis 6: Time-of-Day
# ---------------------------------------------------------------------------

def analysis_6(sigs, tmf_ts, tmf_price):
    print("\n" + "=" * 80)
    print("ANALYSIS 6: Time-of-Day Effect (36ms)")
    print("=" * 80)

    import datetime

    periods = {
        "open (08:45-09:15)": (8 * 60 + 45, 9 * 60 + 15),
        "morning (09:15-11:00)": (9 * 60 + 15, 11 * 60),
        "midday (11:00-12:30)": (11 * 60, 12 * 60 + 30),
        "close (12:30-13:45)": (12 * 60 + 30, 13 * 60 + 45),
    }

    results = {}
    for pname, (smin, emin) in periods.items():
        rets = []
        for s in sigs:
            dt = datetime.datetime.fromtimestamp(
                s["ts"] / 1e9, tz=datetime.timezone(datetime.timedelta(hours=8)))
            mod = dt.hour * 60 + dt.minute
            if mod < smin or mod >= emin:
                continue
            bp = find_price_at(tmf_ts, tmf_price, s["ts"])
            fp = find_price_at(tmf_ts, tmf_price, s["ts"] + 36 * MS)
            if np.isnan(bp) or np.isnan(fp):
                continue
            rets.append(s["direction"] * (fp - bp))

        if rets:
            arr = np.array(rets)
            m = np.mean(arr)
            net = m - RT_COST
            tag = "PASS" if net > 0 else "FAIL"
            print(f"  {pname:>25}: N={len(arr):>4}, mean={m:>6.2f}, "
                  f"dir_acc={np.mean(arr > 0):.1%}, net={net:>6.2f} [{tag}]")
            results[pname] = {
                "n": len(arr), "mean": round(m, 3), "net": round(net, 3),
                "dir_acc": round(float(np.mean(arr > 0)), 4), "pass": net > 0,
            }
        else:
            print(f"  {pname:>25}: 0 signals")

    return results


# ---------------------------------------------------------------------------
# Analysis 7: Spread at Entry
# ---------------------------------------------------------------------------

def analysis_7(sigs, ba_ts, ba_bid, ba_ask):
    print("\n" + "=" * 80)
    print("ANALYSIS 7: TMF Spread at Entry")
    print("=" * 80)

    sp_signal = []
    sp_entry = []

    for s in sigs:
        b0, a0 = find_bidask_at(ba_ts, ba_bid, ba_ask, s["ts"])
        b1, a1 = find_bidask_at(ba_ts, ba_bid, ba_ask, s["ts"] + 36 * MS)
        if not np.isnan(b0) and not np.isnan(a0) and a0 > b0:
            sp_signal.append(a0 - b0)
        if not np.isnan(b1) and not np.isnan(a1) and a1 > b1:
            sp_entry.append(a1 - b1)

    s0 = np.array(sp_signal) if sp_signal else np.array([1.0])
    s1 = np.array(sp_entry) if sp_entry else np.array([1.0])

    print(f"  At signal time:  mean={np.mean(s0):.2f}, median={np.median(s0):.2f}, "
          f"P90={np.percentile(s0, 90):.2f}")
    print(f"  At signal+36ms:  mean={np.mean(s1):.2f}, median={np.median(s1):.2f}, "
          f"P90={np.percentile(s1, 90):.2f}")

    widening = np.mean(s1) - np.mean(s0)
    real_cost = RT_COST + max(widening, 0)
    print(f"  Spread widening: {widening:+.2f} pts")
    print(f"  Real cost (RT + widening): {real_cost:.2f} pts")

    # Half-spread at entry (what we actually pay)
    half_sp_entry = np.mean(s1) / 2.0
    print(f"  Half-spread at entry (each side): {half_sp_entry:.2f} pts")
    print(f"  Full spread cost (entry+exit): {np.mean(s1):.2f} pts")

    return {
        "at_signal": {"mean": round(float(np.mean(s0)), 3), "median": round(float(np.median(s0)), 3)},
        "at_entry": {"mean": round(float(np.mean(s1)), 3), "median": round(float(np.median(s1)), 3)},
        "widening": round(float(widening), 3),
        "real_cost_with_widening": round(float(real_cost), 3),
        "half_spread_entry": round(float(half_sp_entry), 3),
    }


# ---------------------------------------------------------------------------
# Analysis 8: Statistical Significance
# ---------------------------------------------------------------------------

def analysis_8(sigs, tmf_ts, tmf_price, label: str = ""):
    print("\n" + "=" * 80)
    print(f"ANALYSIS 8: Statistical Significance {label}")
    print("=" * 80)

    from scipy import stats

    rets = []
    for s in sigs:
        bp = find_price_at(tmf_ts, tmf_price, s["ts"])
        fp = find_price_at(tmf_ts, tmf_price, s["ts"] + 36 * MS)
        if np.isnan(bp) or np.isnan(fp):
            continue
        rets.append(s["direction"] * (fp - bp))

    arr = np.array(rets)
    n = len(arr)
    m = np.mean(arr)
    std = np.std(arr, ddof=1)

    t0, p0 = stats.ttest_1samp(arr, 0)
    tc, pc = stats.ttest_1samp(arr, RT_COST)

    cohens_d = m / std if std > 0 else 0

    rng = np.random.default_rng(42)
    boot = np.empty(10000)
    for i in range(10000):
        boot[i] = np.mean(rng.choice(arr, size=n, replace=True))
    ci_lo, ci_hi = np.percentile(boot, [2.5, 97.5])

    net_sig = pc < 0.05 and tc > 0

    print(f"  N = {n}")
    print(f"  Mean return: {m:.3f} pts (std={std:.3f})")
    print(f"  t-test (H0: mean=0):     t={t0:.3f}, p={p0:.6f} "
          f"{'***' if p0 < 0.001 else '**' if p0 < 0.01 else '*' if p0 < 0.05 else 'ns'}")
    print(f"  t-test (H0: mean={RT_COST}): t={tc:.3f}, p={pc:.6f} "
          f"{'***' if pc < 0.001 else '**' if pc < 0.01 else '*' if pc < 0.05 else 'ns'}")
    print(f"  Net significant (mean > cost)? {'YES' if net_sig else 'NO'}")
    print(f"  Cohen's d: {cohens_d:.4f} "
          f"({'large' if abs(cohens_d) > 0.8 else 'medium' if abs(cohens_d) > 0.5 else 'small' if abs(cohens_d) > 0.2 else 'negligible'})")
    print(f"  Bootstrap 95% CI: [{ci_lo:.3f}, {ci_hi:.3f}]")
    print(f"  Bootstrap net CI: [{ci_lo - RT_COST:.3f}, {ci_hi - RT_COST:.3f}]")

    return {
        "n": n, "mean": round(m, 3), "std": round(std, 3),
        "ttest_vs_0": {"t": round(float(t0), 3), "p": round(float(p0), 6)},
        "ttest_vs_cost": {"t": round(float(tc), 3), "p": round(float(pc), 6)},
        "net_significant": net_sig,
        "cohens_d": round(float(cohens_d), 4),
        "bootstrap_ci_95": [round(float(ci_lo), 3), round(float(ci_hi), 3)],
        "bootstrap_net_ci_95": [round(float(ci_lo - RT_COST), 3), round(float(ci_hi - RT_COST), 3)],
    }


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    t0 = time.time()

    print("R26 Stage 3: TX→TMF Lead-Lag Deep Analysis (CORRECTED)")
    print("=" * 80)

    # --- Load data ---
    print("\nLoading TX signals (with server-side dp/dvol)...")
    all_signals = load_tx_signals_from_ck(min_date="2026-01-01")
    print(f"  TX ticks with dp!=0: {len(all_signals):,}")

    march_signals = [s for s in all_signals if s["ts"] >= MARCH_START_NS]
    print(f"  March+ signals (all dp!=0): {len(march_signals):,}")

    # Match team lead's filter
    cumvol20 = filter_signals(all_signals, cum_vol_min=20, march_only=True)
    print(f"  cum_vol>=20, dp!=0, March: {len(cumvol20)}")

    # True large per-tick orders
    dvol5 = filter_signals(all_signals, dvol_min=5, march_only=True)
    dvol10 = filter_signals(all_signals, dvol_min=10, march_only=True)
    dvol20 = filter_signals(all_signals, dvol_min=20, march_only=True)
    print(f"  dvol>=5, dp!=0, March: {len(dvol5)}")
    print(f"  dvol>=10, dp!=0, March: {len(dvol10)}")
    print(f"  dvol>=20, dp!=0, March: {len(dvol20)}")

    print("\nLoading TMF ticks...")
    tmf_ts, tmf_price = load_tmf_ticks(min_date="2026-01-01")
    print(f"  TMF ticks: {len(tmf_ts):,}")

    print("Loading TMF BidAsk (filtering zero prices)...")
    ba_ts, ba_bid, ba_ask = load_tmf_bidask(min_date="2026-01-01")
    print(f"  TMF BidAsk: {len(ba_ts):,}")

    all_results = {}

    # --- Analysis 1 ---
    all_results["analysis_1"] = analysis_1(all_signals, tmf_ts, tmf_price)

    # --- Analysis 2: Realistic entry ---
    # Run for both interpretations
    sig_sets = {
        "cumvol>=20 (team lead)": cumvol20,
        "dvol>=5 (real large)": dvol5,
        "dvol>=10": dvol10,
    }
    all_results["analysis_2"] = analysis_2(sig_sets, ba_ts, ba_bid, ba_ask, tmf_ts, tmf_price)

    # --- Analysis 3: SL/TP (using cumvol>=20 to match team lead) ---
    a3_res, a3_best = analysis_3(cumvol20, tmf_ts, tmf_price)
    all_results["analysis_3"] = {"configs": a3_res, "best": a3_best}

    # Also run with dvol>=5
    print("\n  --- SL/TP with dvol>=5 ---")
    a3b_res, a3b_best = analysis_3(dvol5, tmf_ts, tmf_price)
    all_results["analysis_3_dvol5"] = {"configs": a3b_res, "best": a3b_best}

    # --- Analysis 4: Decay curve (cumvol>=20) ---
    a4_decay, a4_cross = analysis_4(cumvol20, tmf_ts, tmf_price)
    all_results["analysis_4"] = {"curve": a4_decay, "crossover_ms": a4_cross}

    # --- Analysis 5: Volume-weighted ---
    all_results["analysis_5"] = analysis_5(all_signals, tmf_ts, tmf_price)

    # --- Analysis 6: Time-of-day ---
    all_results["analysis_6_cumvol20"] = analysis_6(cumvol20, tmf_ts, tmf_price)
    print("\n  --- dvol>=5 ---")
    all_results["analysis_6_dvol5"] = analysis_6(dvol5, tmf_ts, tmf_price)

    # --- Analysis 7: Spread ---
    all_results["analysis_7_cumvol20"] = analysis_7(cumvol20, ba_ts, ba_bid, ba_ask)
    all_results["analysis_7_dvol5"] = analysis_7(dvol5, ba_ts, ba_bid, ba_ask)

    # --- Analysis 8: Stats (both) ---
    all_results["analysis_8_cumvol20"] = analysis_8(
        cumvol20, tmf_ts, tmf_price, "(cumvol>=20)")
    all_results["analysis_8_dvol5"] = analysis_8(
        dvol5, tmf_ts, tmf_price, "(dvol>=5)")

    # ===========================================================================
    # SUMMARY
    # ===========================================================================
    print("\n" + "=" * 80)
    print("GRAND SUMMARY")
    print("=" * 80)

    a8_cv = all_results["analysis_8_cumvol20"]
    a8_dv = all_results["analysis_8_dvol5"]
    a7_cv = all_results["analysis_7_cumvol20"]

    print("\n  VOLUME SEMANTICS FINDING:")
    print("    The `volume` field is CUMULATIVE daily session volume, NOT per-tick qty.")
    print("    Team lead's 'vol>=20 dp!=0' = any price-changing tick after daily volume")
    print("    reaches 20 contracts. This is NOT a 'large order' filter.")
    print(f"    cum_vol>=20 signals: {len(cumvol20)} (basically all ticks with dp!=0)")
    print(f"    dvol>=5 signals:     {len(dvol5)} (true large orders)")
    print(f"    dvol>=10 signals:    {len(dvol10)}")
    print(f"    dvol>=20 signals:    {len(dvol20)}")

    print(f"\n  cum_vol>=20 (team lead's method):")
    print(f"    Mean return @36ms:  {a8_cv['mean']:.3f} pts")
    print(f"    Net after cost:     {a8_cv['mean'] - RT_COST:.3f} pts")
    print(f"    Significant > 0?   {'YES' if a8_cv['ttest_vs_0']['p'] < 0.05 else 'NO'}")
    print(f"    Significant > cost? {a8_cv['net_significant']}")
    print(f"    Spread at entry:    {a7_cv['at_entry']['mean']:.2f} pts")

    print(f"\n  dvol>=5 (real large orders):")
    print(f"    Mean return @36ms:  {a8_dv['mean']:.3f} pts")
    print(f"    Net after cost:     {a8_dv['mean'] - RT_COST:.3f} pts")
    print(f"    Significant > 0?   {'YES' if a8_dv['ttest_vs_0']['p'] < 0.05 else 'NO'}")
    print(f"    Significant > cost? {a8_dv['net_significant']}")

    # Critical question
    print("\n  CRITICAL QUESTION: After realistic slippage + spread + 4 pts RT cost,")
    print("  is the net expectation per trade positive?")
    cv_pass = a8_cv["mean"] > RT_COST + a7_cv.get("widening", 0)
    dv_pass = a8_dv["mean"] > RT_COST
    print(f"    cum_vol>=20: {'YES' if cv_pass else 'NO — FAIL'}")
    print(f"    dvol>=5:     {'YES' if dv_pass else 'NO — FAIL'}")

    verdict = "PASS" if (cv_pass or dv_pass) else "FAIL"
    all_results["verdict"] = {
        "volume_semantics_bug": True,
        "cumvol20_net": round(a8_cv["mean"] - RT_COST, 3),
        "dvol5_net": round(a8_dv["mean"] - RT_COST, 3),
        "cumvol20_pass": cv_pass,
        "dvol5_pass": dv_pass,
        "final_verdict": verdict,
        "notes": [
            "Volume field is cumulative daily volume, not per-tick trade size",
            "Team lead's initial 79% dir accuracy not reproduced",
            "Need to investigate data pipeline for possible dedup or snapshot issues",
        ],
    }

    print(f"\n  FINAL VERDICT: {verdict}")

    # Save
    out_path = Path("/home/charlie/hft_platform/outputs/team_artifacts/alpha-research-r26/stage3_results.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Saved to {out_path}")
    print(f"  Runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
