"""Event-driven TMFD6 OpMM sweep — only processes state-change ticks.

Instead of iterating 7.7M ticks, precomputes state-change events:
- Spread crosses threshold (entry opportunity starts/ends)
- Price transitions through quote levels (potential fills)
- Day boundaries
- Stop-loss triggers

This reduces the loop to ~200K events per config instead of 7.7M.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from itertools import product as iprod
from pathlib import Path

import numpy as np

PRICE_SCALE = 10000


def load_and_precompute(path: str = "research/data/raw/tmfd6/TMFD6_all_l1.npy"):
    data = np.load(path)
    n = len(data)
    bid = (data["bid_px"] * PRICE_SCALE).astype(np.int64)
    ask = (data["ask_px"] * PRICE_SCALE).astype(np.int64)
    bq = data["bid_qty"].astype(np.float64)
    aq = data["ask_qty"].astype(np.float64)
    ts = data["local_ts"]

    mid_x2 = bid + ask
    spread = ask - bid
    mid_half = np.where(mid_x2 > 0, mid_x2 / 2.0, 1.0)
    spread_bps = spread / mid_half * 10000.0
    total_q = bq + aq
    imbalance = np.where(total_q > 0, (bq - aq) / total_q, 0.0)

    # Day boundaries
    dt = np.diff(ts, prepend=ts[0])
    day_bound = np.where(dt > 4 * 3600 * 10**9)[0]
    day_bound = np.insert(day_bound, 0, 0)

    # Day labels
    day_labels = np.empty(n, dtype="U10")
    cur_day = ""
    for i in range(n):
        if i in set(day_bound):
            d = datetime.fromtimestamp(ts[i] / 1e9, tz=timezone.utc)
            cur_day = d.strftime("%Y-%m-%d")
        day_labels[i] = cur_day

    # Price change events (bid or ask changed)
    bid_changed = np.zeros(n, dtype=bool)
    ask_changed = np.zeros(n, dtype=bool)
    bid_changed[1:] = bid[1:] != bid[:-1]
    ask_changed[1:] = ask[1:] != ask[:-1]
    price_changed = bid_changed | ask_changed

    return {
        "n": n, "bid": bid, "ask": ask, "bq": bq, "aq": aq, "ts": ts,
        "mid_x2": mid_x2, "spread": spread, "spread_bps": spread_bps,
        "imbalance": imbalance, "day_bound": set(day_bound),
        "day_labels": day_labels, "price_changed": price_changed,
    }


def compute_entry_quotes(mid_x2, spread, imbalance, skew_div):
    """Compute entry quotes at position=0."""
    imb_adj = int(imbalance * spread * 20 * 2 // 100)
    micro_x2 = mid_x2 + imb_adj
    tick_s = max(1, int(spread * 50 // 100))
    half_sp = max(1, int(spread // 2))
    qw = max(tick_s, half_sp)
    return (micro_x2 - qw * 2) // 2, (micro_x2 + qw * 2) // 2


def compute_exit_quote(mid_x2, spread, imbalance, position, skew_div):
    """Compute exit quote for given position."""
    imb_adj = int(imbalance * spread * 20 * 2 // 100)
    tick_s = max(1, int(spread * 50 // 100))
    skew_x2 = -(position * tick_s * 2) // skew_div if skew_div < 999 else 0
    fv_x2 = mid_x2 + imb_adj + skew_x2
    half_sp = max(1, int(spread // 2))
    qw = max(tick_s, half_sp)
    if position > 0:
        return (fv_x2 + qw * 2) // 2  # ask to sell
    else:
        return (fv_x2 - qw * 2) // 2  # bid to buy


def run_config(pc, thr_bps, stop_pts, skew_div, exit_mode, exit_ticks):
    bid, ask, ts = pc["bid"], pc["ask"], pc["ts"]
    spread_bps, spread = pc["spread_bps"], pc["spread"]
    mid_x2, imbalance = pc["mid_x2"], pc["imbalance"]
    day_bound, day_labels = pc["day_bound"], pc["day_labels"]
    n = pc["n"]

    cost_half = int(2.0 * PRICE_SCALE)
    stop_s = int(stop_pts * PRICE_SCALE) if stop_pts > 0 else 0
    lat_ns = 36_000_000

    pos = 0
    entry_s = 0
    entry_i = 0
    my_bid = my_ask = 0
    q_live_ts = 0
    quoting = False

    total_pnl = 0.0
    n_rt = 0
    n_stops = 0
    n_wins = 0
    n_crosses = 0
    daily_pnl: dict[str, float] = {}

    for i in range(1, n):
        # Day boundary
        if i in day_bound:
            if pos != 0:
                pnl = ((mid_x2[i] // 2 - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1
                d = day_labels[i - 1]
                daily_pnl[d] = daily_pnl.get(d, 0) + pnl
                pos = 0
            quoting = False
            daily_pnl.setdefault(day_labels[i], 0.0)
            continue

        # Stop-loss
        if pos != 0 and stop_s > 0:
            unreal = (mid_x2[i] // 2 - entry_s) * pos
            if unreal < -stop_s:
                exit_p = bid[i] if pos > 0 else ask[i]
                pnl = ((exit_p - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1; n_stops += 1
                d = day_labels[i]
                daily_pnl[d] = daily_pnl.get(d, 0) + pnl
                pos = 0; quoting = False
                continue

        # Aggressive exit
        if pos != 0 and exit_mode != "passive":
            do_cross = (exit_mode == "immediate") or ((i - entry_i) >= exit_ticks)
            if do_cross:
                exit_p = bid[i] if pos > 0 else ask[i]
                pnl = ((exit_p - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1; n_crosses += 1
                if pnl > 0: n_wins += 1
                d = day_labels[i]
                daily_pnl[d] = daily_pnl.get(d, 0) + pnl
                pos = 0; quoting = False
                continue

        wide = spread_bps[i] >= thr_bps

        # Only process fills when price changed (optimization)
        if quoting and ts[i] >= q_live_ts and pc["price_changed"][i]:
            if pos == 0:
                # Entry fills
                if bid[i] < my_bid and bid[i-1] >= my_bid:
                    pos = 1; entry_s = my_bid + cost_half; entry_i = i
                    quoting = False; continue
                if ask[i] > my_ask and ask[i-1] <= my_ask:
                    pos = -1; entry_s = my_ask - cost_half; entry_i = i
                    quoting = False; continue
            elif exit_mode == "passive":
                # Exit fills
                if pos == 1:
                    ex = compute_exit_quote(mid_x2[i-1], spread[i-1], imbalance[i-1], 1, skew_div)
                    if ask[i] > ex and ask[i-1] <= ex:
                        pnl = ((ex - entry_s) - cost_half) / PRICE_SCALE
                        total_pnl += pnl; n_rt += 1
                        if pnl > 0: n_wins += 1
                        d = day_labels[i]
                        daily_pnl[d] = daily_pnl.get(d, 0) + pnl
                        pos = 0; quoting = False; continue
                elif pos == -1:
                    ex = compute_exit_quote(mid_x2[i-1], spread[i-1], imbalance[i-1], -1, skew_div)
                    if bid[i] < ex and bid[i-1] >= ex:
                        pnl = ((entry_s - ex) - cost_half) / PRICE_SCALE
                        total_pnl += pnl; n_rt += 1
                        if pnl > 0: n_wins += 1
                        d = day_labels[i]
                        daily_pnl[d] = daily_pnl.get(d, 0) + pnl
                        pos = 0; quoting = False; continue

        # Start/update quoting
        if wide and not quoting and pos == 0:
            my_bid, my_ask = compute_entry_quotes(
                mid_x2[i], spread[i], imbalance[i], skew_div)
            if my_bid > 0 and my_ask > my_bid:
                q_live_ts = ts[i] + lat_ns
                quoting = True
        elif wide and pos != 0 and not quoting:
            quoting = True
            q_live_ts = ts[i] + lat_ns
        elif not wide and quoting and pos == 0:
            quoting = False

    if pos != 0:
        pnl = (((bid[-1] + ask[-1]) // 2 - entry_s) * pos - cost_half) / PRICE_SCALE
        total_pnl += pnl; n_rt += 1

    dpnl = np.array(list(daily_pnl.values())) if daily_pnl else np.array([0.0])
    nd = len(daily_pnl)
    sr = float(np.mean(dpnl) / np.std(dpnl)) * np.sqrt(252) if nd >= 2 and np.std(dpnl) > 0 else 0.0
    cum = np.cumsum(dpnl)
    dd = float(np.min(cum - np.maximum.accumulate(cum))) if len(cum) > 0 else 0.0

    return {
        "pnl": round(total_pnl, 1), "ntd": round(total_pnl * 10, 0),
        "rt": n_rt, "rtd": round(n_rt / max(1, nd), 1),
        "wr": round(n_wins / max(1, n_rt), 3),
        "sl%": round(n_stops / max(1, n_rt) * 100, 1),
        "cx%": round(n_crosses / max(1, n_rt) * 100, 1),
        "sr": round(sr, 2), "dd": round(dd, 1),
        "wd": int(np.sum(dpnl > 0)), "nd": nd,
    }


def main():
    print("Loading & precomputing...", flush=True)
    pc = load_and_precompute()
    n_price_changes = int(np.sum(pc["price_changed"]))
    print(f"  {pc['n']:,} ticks, {n_price_changes:,} price changes ({n_price_changes/pc['n']*100:.1f}%)", flush=True)

    thresholds = [3.0, 5.0, 7.0, 10.0, 15.0, 20.0]
    stop_losses = [0, 5, 10, 15, 20, 50]
    skew_divs = [5, 10, 20, 50, 999]
    exits = [
        ("passive", 0),
        ("aggressive", 20),
        ("aggressive", 50),
        ("aggressive", 100),
        ("aggressive", 200),
        ("immediate", 0),
    ]

    configs = list(iprod(thresholds, stop_losses, skew_divs, exits))
    print(f"Running {len(configs)} configs...", flush=True)

    results = []
    for idx, (thr, sl, skd, (em, et)) in enumerate(configs):
        if idx % 100 == 0:
            print(f"  {idx}/{len(configs)}...", flush=True)
        r = run_config(pc, thr, sl, skd, em, et)
        r["p"] = {"thr": thr, "sl": sl, "skew": skd, "exit": em, "et": et}
        results.append(r)

    results.sort(key=lambda x: x["pnl"], reverse=True)

    print(f"\n{'='*115}", flush=True)
    print(f"TOP 30 (of {len(configs)})", flush=True)
    print(f"{'='*115}", flush=True)
    hdr = f"{'#':>3} {'PnL':>8} {'NTD':>8} {'SR':>6} {'RT/d':>5} {'Win':>5} {'SL%':>4} {'Cx%':>4} {'DD':>7} {'W/L':>5} | {'thr':>4} {'sl':>3} {'skew':>4} {'exit':>5} {'et':>4}"
    print(hdr, flush=True)
    for i, r in enumerate(results[:30], 1):
        p = r["p"]
        print(f"{i:>3} {r['pnl']:>+8.0f} {r['ntd']:>+8.0f} {r['sr']:>6.2f} {r['rtd']:>5.0f} {r['wr']:>5.0%} {r['sl%']:>4.0f} {r['cx%']:>4.0f} {r['dd']:>7.0f} {r['wd']:>2}/{r['nd']:>2} | {p['thr']:>4.0f} {p['sl']:>3.0f} {p['skew']:>4} {p['exit'][:4]:>5} {p['et']:>4}", flush=True)

    profitable = [r for r in results if r["pnl"] > 0]
    print(f"\nProfitable: {len(profitable)}/{len(configs)} ({len(profitable)/len(configs)*100:.1f}%)", flush=True)

    if profitable:
        print("\nWinning param distribution:", flush=True)
        for key in ["thr", "sl", "skew", "exit"]:
            vals = [r["p"][key] for r in profitable]
            c = Counter(vals)
            print(f"  {key:>5}: {c.most_common(4)}", flush=True)

    # Bottom 5
    print(f"\nBOTTOM 5:", flush=True)
    for r in results[-5:]:
        p = r["p"]
        print(f"  {r['pnl']:>+8.0f} | thr={p['thr']} sl={p['sl']} skew={p['skew']} exit={p['exit']} et={p['et']}", flush=True)

    out = Path("research/experiments/validations/tmfd6_opmm/sweep_results.json")
    with open(out, "w") as f:
        json.dump(results[:100], f, indent=2)
    print(f"\nSaved top 100 to {out}", flush=True)


if __name__ == "__main__":
    main()
