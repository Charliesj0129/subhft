"""R47 Parameter Optimization — Grid search on cross-instrument backtest.

Sweeps: spread_threshold × pe_danger × queue_threshold
Commission: 1.3 pts/side (TMFD6 confirmed)

Usage:
    uv run python research/tools/r47_param_sweep.py
"""
from __future__ import annotations

import itertools
import math
import os
import sys

import clickhouse_connect
import numpy as np

COMMISSION = 1.3  # pts per side
CK_SCALE = 1_000_000
TMFD6_PV = 10  # NTD/pt

# Parameter grid
SPREAD_THRESHOLDS = [3, 4, 5, 6, 7]
PE_DANGERS = [0.0, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]  # 0.0 = disabled
QUEUE_THRESHOLDS = [0.0, 0.5, 0.6, 0.7, 0.8, 0.9]  # 0.0 = disabled

DATES = [
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24",
    "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31",
    "2026-04-01", "2026-04-02", "2026-04-07", "2026-04-08",
]


def get_ch():
    return clickhouse_connect.get_client(
        host="localhost", port=8123, username="default", password="changeme"
    )


def load_ba(client, symbol, date):
    r = client.query(f"""
        SELECT exch_ts, bids_price[1], bids_vol[1], asks_price[1], asks_vol[1]
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'BidAsk'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date}'
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
    """)
    rows = r.result_rows
    if not rows:
        return None
    return {
        "ts": np.array([x[0] for x in rows], dtype=np.int64),
        "bp": np.array([x[1] for x in rows], dtype=np.int64),
        "bv": np.array([x[2] for x in rows], dtype=np.int64),
        "ap": np.array([x[3] for x in rows], dtype=np.int64),
        "av": np.array([x[4] for x in rows], dtype=np.int64),
    }


def load_ticks(client, symbol, date):
    r = client.query(f"""
        SELECT exch_ts, price_scaled, volume, trade_direction
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'Tick'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date}'
          AND price_scaled > 0
        ORDER BY exch_ts
    """)
    rows = r.result_rows
    if not rows:
        return {"ts": np.array([], dtype=np.int64), "dir": np.array([], dtype=np.int8),
                "vol": np.array([], dtype=np.int64)}
    ts = np.array([x[0] for x in rows], dtype=np.int64)
    px = np.array([x[1] for x in rows], dtype=np.int64)
    d = np.array([x[3] for x in rows], dtype=np.int8)
    v = np.array([x[2] for x in rows], dtype=np.int64)
    for i in range(1, len(d)):
        if d[i] == 0:
            diff = px[i] - px[i - 1]
            d[i] = 1 if diff > 0 else (-1 if diff < 0 else d[i - 1])
    return {"ts": ts, "dir": d, "vol": v}


def _lehmer(vals):
    d = len(vals)
    ranks = [0] * d
    for i in range(d):
        for j in range(d):
            if vals[j] < vals[i] or (vals[j] == vals[i] and j < i):
                ranks[i] += 1
    idx = 0; f = 1; facts = [1] * d
    for i in range(d - 1, -1, -1):
        facts[i] = f; f *= (d - i)
    for i in range(d):
        c = sum(1 for j in range(i + 1, d) if ranks[j] < ranks[i])
        idx += c * facts[i]
    return idx


def compute_pe(qi, d=4, w=100):
    n = len(qi); h_out = np.ones(n)
    if n < w: return h_out
    np_pat = math.factorial(d); h_max = math.log2(np_pat); ppw = w - d + 1
    pats = np.empty(n - d + 1, dtype=np.int32)
    for i in range(len(pats)):
        pats[i] = _lehmer(qi[i:i + d].tolist())
    counts = np.bincount(pats[:ppw], minlength=np_pat).astype(np.float64)
    step = max(1, (len(pats) - ppw + 1) // 20_000)
    def _h(c, ns):
        p = c[c > 0] / ns; return -np.sum(p * np.log2(p)) / h_max
    hv = _h(counts, ppw)
    for i in range(len(pats) - ppw + 1):
        if i > 0:
            counts[pats[i - 1]] -= 1; counts[pats[i + ppw - 1]] += 1
        if i % step == 0: hv = _h(counts, ppw)
        idx = i + w - 1
        if idx < n: h_out[idx] = hv
    return h_out


def compute_queue(bv, av, alpha=0.05):
    n = len(bv); pb = np.full(n, 0.5); pa = np.full(n, 0.5)
    lb = mb = la = ma = 1.0
    for i in range(1, n):
        db = int(bv[i]) - int(bv[i - 1]); da = int(av[i]) - int(av[i - 1])
        if db > 0: lb = alpha * db + (1 - alpha) * lb
        elif db < 0: mb = alpha * (-db) + (1 - alpha) * mb
        if da > 0: la = alpha * da + (1 - alpha) * la
        elif da < 0: ma = alpha * (-da) + (1 - alpha) * ma
        rb = mb / max(lb, 1e-6); ra = ma / max(la, 1e-6)
        pb[i] = min(1.0, rb ** max(int(bv[i]), 1))
        pa[i] = min(1.0, ra ** max(int(av[i]), 1))
    return pb, pa


def sweep_one_day(sig_ba, exec_ba, h_arr, pb, pa,
                  spr_thresh, pe_danger, q_thresh):
    """Run one parameter combo on one day, return (n_fills, total_net_pts, n_profitable)."""
    sig_ts = sig_ba["ts"]
    exec_ts = exec_ba["ts"]
    exec_bp = exec_ba["bp"]; exec_bv = exec_ba["bv"]
    exec_ap = exec_ba["ap"]; exec_av = exec_ba["av"]
    n = len(exec_ts)

    exec_spread = (exec_ap.astype(np.float64) - exec_bp.astype(np.float64)) / CK_SCALE
    exec_mid = (exec_bp.astype(np.float64) + exec_ap.astype(np.float64)) / 2.0
    sig_idx_map = np.clip(np.searchsorted(sig_ts, exec_ts, side="right") - 1, 0, len(sig_ts) - 1)

    n_fills = 0
    total_net = 0.0
    n_profitable = 0

    for i in range(1, n - 100):
        if exec_spread[i] < spr_thresh:
            continue
        si = sig_idx_map[i]

        if pe_danger > 0 and h_arr[si] < pe_danger:
            continue

        sup_bid = q_thresh > 0 and pb[si] > q_thresh
        sup_ask = q_thresh > 0 and pa[si] > q_thresh

        # Bid fill
        if (not sup_bid and exec_bp[i] == exec_bp[i - 1]
                and exec_bv[i] < exec_bv[i - 1] and exec_bv[i - 1] > 0):
            fwd = np.searchsorted(exec_ts, exec_ts[i] + 1_000_000_000)
            if fwd < n:
                pnl = (exec_mid[fwd] - exec_bp[i]) / CK_SCALE - COMMISSION
                n_fills += 1
                total_net += pnl
                if pnl > 0:
                    n_profitable += 1

        # Ask fill
        if (not sup_ask and exec_ap[i] == exec_ap[i - 1]
                and exec_av[i] < exec_av[i - 1] and exec_av[i - 1] > 0):
            fwd = np.searchsorted(exec_ts, exec_ts[i] + 1_000_000_000)
            if fwd < n:
                pnl = (exec_ap[i] - exec_mid[fwd]) / CK_SCALE - COMMISSION
                n_fills += 1
                total_net += pnl
                if pnl > 0:
                    n_profitable += 1

    return n_fills, total_net, n_profitable


def main():
    print("=" * 80, flush=True)
    print("R47 Parameter Optimization — Grid Search", flush=True)
    print(f"Grid: {len(SPREAD_THRESHOLDS)} spread × {len(PE_DANGERS)} PE × "
          f"{len(QUEUE_THRESHOLDS)} queue = "
          f"{len(SPREAD_THRESHOLDS) * len(PE_DANGERS) * len(QUEUE_THRESHOLDS)} combos", flush=True)
    print(f"Commission: {COMMISSION} pts/side | 12 days TXFD6→TMFD6", flush=True)
    print("=" * 80, flush=True)

    client = get_ch()

    # Pre-load all data
    print("\nLoading data...", flush=True)
    day_data = []
    for date in DATES:
        sig_ba = load_ba(client, "TXFD6", date)
        exec_ba = load_ba(client, "TMFD6", date)
        sig_tk = load_ticks(client, "TXFD6", date)
        if sig_ba is None or exec_ba is None or len(sig_ba["ts"]) < 1000 or len(exec_ba["ts"]) < 1000:
            continue
        total_vol = sig_ba["bv"] + sig_ba["av"]
        qi = np.where(total_vol > 0,
                      (sig_ba["bv"].astype(np.float64) - sig_ba["av"].astype(np.float64)) / total_vol, 0.0)
        h_arr = compute_pe(qi)
        pb, pa = compute_queue(sig_ba["bv"], sig_ba["av"])
        day_data.append({"date": date, "sig_ba": sig_ba, "exec_ba": exec_ba,
                         "h": h_arr, "pb": pb, "pa": pa})
        print(f"  {date}: ready", flush=True)

    print(f"\n{len(day_data)} days loaded. Running grid search...\n", flush=True)

    # Grid search
    results = []
    total_combos = len(SPREAD_THRESHOLDS) * len(PE_DANGERS) * len(QUEUE_THRESHOLDS)
    done = 0

    for spr in SPREAD_THRESHOLDS:
        for pe_d in PE_DANGERS:
            for qt in QUEUE_THRESHOLDS:
                tot_fills = 0
                tot_net = 0.0
                tot_prof = 0
                day_nets = []

                for dd in day_data:
                    nf, tn, np_ = sweep_one_day(
                        dd["sig_ba"], dd["exec_ba"], dd["h"], dd["pb"], dd["pa"],
                        spr, pe_d, qt,
                    )
                    tot_fills += nf
                    tot_net += tn
                    tot_prof += np_
                    day_nets.append(tn)

                win_rate = tot_prof / tot_fills * 100 if tot_fills > 0 else 0
                ntd = tot_net * TMFD6_PV
                avg_net = tot_net / tot_fills if tot_fills > 0 else 0
                profitable_days = sum(1 for x in day_nets if x > 0)

                # Sharpe proxy: mean daily net / std daily net
                if len(day_nets) > 1 and np.std(day_nets) > 0:
                    sharpe = np.mean(day_nets) / np.std(day_nets) * math.sqrt(252)
                else:
                    sharpe = 0.0

                results.append({
                    "spr": spr, "pe_d": pe_d, "qt": qt,
                    "fills": tot_fills, "net": tot_net, "ntd": ntd,
                    "avg_net": avg_net, "win%": win_rate,
                    "prof_days": profitable_days, "sharpe": sharpe,
                    "day_nets": day_nets,
                })

                done += 1
                if done % 30 == 0:
                    print(f"  {done}/{total_combos} combos done...", flush=True)

    # Sort by total net PnL
    results.sort(key=lambda r: r["net"], reverse=True)

    # Print top 20
    print("\n" + "=" * 80, flush=True)
    print("TOP 20 PARAMETER COMBINATIONS (by total net PnL)", flush=True)
    print("=" * 80, flush=True)
    print(f"{'Rank':>4} {'Spr':>4} {'PE_d':>5} {'Q_th':>5} "
          f"{'Fills':>7} {'AvgNet':>8} {'Win%':>6} "
          f"{'TotNet':>10} {'NTD':>10} {'PrDays':>7} {'Sharpe':>7}", flush=True)
    print("-" * 85, flush=True)

    for i, r in enumerate(results[:20]):
        pe_str = f"{r['pe_d']:.2f}" if r["pe_d"] > 0 else " off"
        qt_str = f"{r['qt']:.1f}" if r["qt"] > 0 else " off"
        print(f"{i+1:>4} {r['spr']:>4} {pe_str:>5} {qt_str:>5} "
              f"{r['fills']:>7,} {r['avg_net']:>+8.2f} {r['win%']:>5.1f}% "
              f"{r['net']:>+10.1f} {r['ntd']:>+10,.0f} "
              f"{r['prof_days']:>5}/12 {r['sharpe']:>+7.2f}", flush=True)

    # Print bottom 5 (worst)
    print(f"\nBOTTOM 5:", flush=True)
    for r in results[-5:]:
        pe_str = f"{r['pe_d']:.2f}" if r["pe_d"] > 0 else " off"
        qt_str = f"{r['qt']:.1f}" if r["qt"] > 0 else " off"
        print(f"     {r['spr']:>4} {pe_str:>5} {qt_str:>5} "
              f"{r['fills']:>7,} {r['avg_net']:>+8.2f} {r['win%']:>5.1f}% "
              f"{r['net']:>+10.1f} {r['ntd']:>+10,.0f}", flush=True)

    # Best by Sharpe (min 50 fills)
    valid = [r for r in results if r["fills"] >= 50]
    if valid:
        best_sharpe = max(valid, key=lambda r: r["sharpe"])
        print(f"\nBest Sharpe (≥50 fills): spr={best_sharpe['spr']} "
              f"PE={best_sharpe['pe_d']} Q={best_sharpe['qt']} "
              f"Sharpe={best_sharpe['sharpe']:+.2f} "
              f"fills={best_sharpe['fills']} net={best_sharpe['net']:+.1f}", flush=True)

    # Best by avg net per fill (min 50 fills)
    if valid:
        best_avg = max(valid, key=lambda r: r["avg_net"])
        print(f"Best Avg Net (≥50 fills): spr={best_avg['spr']} "
              f"PE={best_avg['pe_d']} Q={best_avg['qt']} "
              f"avg_net={best_avg['avg_net']:+.2f} "
              f"fills={best_avg['fills']} net={best_avg['net']:+.1f}", flush=True)

    # Recommended config
    print("\n" + "=" * 80, flush=True)
    best = results[0]
    print(f"RECOMMENDED CONFIG (max total net):", flush=True)
    print(f"  spread_threshold_pts: {best['spr']}", flush=True)
    print(f"  pe_danger_threshold: {best['pe_d'] if best['pe_d'] > 0 else 'disabled'}", flush=True)
    print(f"  queue_cancel_threshold: {best['qt'] if best['qt'] > 0 else 'disabled'}", flush=True)
    print(f"  Total net: {best['net']:+.1f} pts = {best['ntd']:+,.0f} NTD over 12 days", flush=True)
    print(f"  Fills: {best['fills']:,}, Avg net: {best['avg_net']:+.2f} pts/fill", flush=True)
    print(f"  Win rate: {best['win%']:.1f}%, Profitable days: {best['prof_days']}/12", flush=True)
    print(f"  Sharpe: {best['sharpe']:+.2f}", flush=True)


if __name__ == "__main__":
    main()
