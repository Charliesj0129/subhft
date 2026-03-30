"""Vectorized TMFD6 OpMM parameter sweep.

Strategy: precompute ALL signals with numpy, then simulate with minimal Python loop
on signal-change events only (not every tick).

Key insight: most ticks have no fill and no state change. We only need to process:
1. Ticks where spread crosses threshold (entry/exit opportunities)
2. Ticks where price moves through our quote (fills)
3. Ticks where stop-loss triggers

Approach:
- Phase 1: Numpy vectorized signal computation (spread_bps, imbalance, quote prices)
- Phase 2: Identify candidate fill events (price transitions through quote levels)
- Phase 3: Simulate position management on events only
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PRICE_SCALE = 10000


def load_data(path: str = "research/data/raw/tmfd6/TMFD6_all_l1.npy"):
    data = np.load(path)
    return {
        "bid": (data["bid_px"] * PRICE_SCALE).astype(np.int64),
        "ask": (data["ask_px"] * PRICE_SCALE).astype(np.int64),
        "bq": data["bid_qty"].astype(np.float64),
        "aq": data["ask_qty"].astype(np.float64),
        "ts": data["local_ts"],
        "n": len(data),
    }


def precompute(d: dict) -> dict:
    """Vectorized precomputation of all signals."""
    bid, ask, bq, aq, ts = d["bid"], d["ask"], d["bq"], d["aq"], d["ts"]
    n = d["n"]

    mid_x2 = bid + ask
    spread = ask - bid
    mid_half = np.where(mid_x2 > 0, mid_x2 / 2.0, 1.0)
    spread_bps = spread / mid_half * 10000.0

    total_q = bq + aq
    imbalance = np.where(total_q > 0, (bq - aq) / total_q, 0.0)

    # Day boundaries (gap > 4 hours)
    dt = np.diff(ts, prepend=ts[0])
    day_boundary = dt > (4 * 3600 * 10**9)
    day_boundary[0] = True

    return {
        **d,
        "mid_x2": mid_x2,
        "spread": spread,
        "spread_bps": spread_bps,
        "imbalance": imbalance,
        "day_boundary": day_boundary,
    }


def compute_quotes_vec(mid_x2: np.ndarray, spread: np.ndarray, imbalance: np.ndarray,
                       skew_div: int) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized quote computation for position=0 (entry quotes only)."""
    imb_adj = (imbalance * spread * 20 * 2 // 100).astype(np.int64)
    micro_x2 = mid_x2 + imb_adj
    tick_s = np.maximum(1, spread * 50 // 100)
    # No skew at position=0
    fv_x2 = micro_x2
    half_sp = np.maximum(1, spread // 2)
    qw = np.maximum(tick_s, half_sp)
    q_bid = (fv_x2 - qw * 2) // 2
    q_ask = (fv_x2 + qw * 2) // 2
    return q_bid, q_ask


def run_config(pc: dict, thr_bps: float, stop_pts: float, skew_div: int,
               exit_mode: str, exit_ticks: int) -> dict:
    """Run one configuration using precomputed signals."""
    bid = pc["bid"]
    ask = pc["ask"]
    ts = pc["ts"]
    spread_bps = pc["spread_bps"]
    spread = pc["spread"]
    mid_x2 = pc["mid_x2"]
    imbalance = pc["imbalance"]
    day_boundary = pc["day_boundary"]
    n = pc["n"]

    cost_half = int(2.0 * PRICE_SCALE)
    stop_s = int(stop_pts * PRICE_SCALE) if stop_pts > 0 else 0
    lat_ns = 36_000_000

    # Precompute entry quotes (position=0)
    q_bid_arr, q_ask_arr = compute_quotes_vec(mid_x2, spread, imbalance, skew_div)

    # Precompute exit quotes with skew (position=+1 and position=-1)
    if skew_div < 999:
        tick_s = np.maximum(1, spread * 50 // 100)
        skew_long_x2 = -(1 * tick_s * 2) // skew_div  # position=+1
        skew_short_x2 = -((-1) * tick_s * 2) // skew_div  # position=-1
    else:
        skew_long_x2 = np.zeros(n, dtype=np.int64)
        skew_short_x2 = np.zeros(n, dtype=np.int64)

    imb_adj = (imbalance * spread * 20 * 2 // 100).astype(np.int64)
    tick_s_arr = np.maximum(1, spread * 50 // 100)
    half_sp = np.maximum(1, spread // 2)
    qw = np.maximum(tick_s_arr, half_sp)

    # Exit ask for long position (sell side)
    fv_long_x2 = mid_x2 + imb_adj + skew_long_x2
    exit_ask_long = (fv_long_x2 + qw * 2) // 2

    # Exit bid for short position (buy side)
    fv_short_x2 = mid_x2 + imb_adj + skew_short_x2
    exit_bid_short = (fv_short_x2 - qw * 2) // 2

    # Wide spread mask
    wide = spread_bps >= thr_bps

    # Simulation
    pos = 0
    entry_s = 0
    entry_i = 0
    q_live_ts = 0
    quoting = False

    total_pnl = 0.0
    n_rt = 0
    n_stops = 0
    n_wins = 0
    n_crosses = 0
    daily_pnl: dict[str, float] = {}
    cur_day = ""

    for i in range(1, n):
        # Day boundary
        if day_boundary[i]:
            if pos != 0:
                pnl = ((mid_x2[i] // 2 - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1
                daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                pos = 0
            quoting = False
            dt_obj = datetime.fromtimestamp(ts[i] / 1e9, tz=timezone.utc)
            cur_day = dt_obj.strftime("%Y-%m-%d")
            daily_pnl.setdefault(cur_day, 0.0)
            continue

        # Stop-loss
        if pos != 0 and stop_s > 0:
            unreal = (mid_x2[i] // 2 - entry_s) * pos
            if unreal < -stop_s:
                exit_p = bid[i] if pos > 0 else ask[i]
                pnl = ((exit_p - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1; n_stops += 1
                daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                pos = 0; quoting = False
                continue

        # Aggressive/immediate exit
        if pos != 0 and exit_mode != "passive":
            do_cross = (exit_mode == "immediate") or ((i - entry_i) >= exit_ticks)
            if do_cross:
                exit_p = bid[i] if pos > 0 else ask[i]
                pnl = ((exit_p - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1; n_crosses += 1
                if pnl > 0: n_wins += 1
                daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                pos = 0; quoting = False
                continue

        # Entry fills (position == 0)
        if pos == 0 and quoting and ts[i] >= q_live_ts:
            my_bid = q_bid_arr[entry_i]  # quote computed at entry_i
            my_ask = q_ask_arr[entry_i]

            # Buy fill: bid dropped through our bid
            if bid[i] < my_bid and bid[i-1] >= my_bid:
                pos = 1
                entry_s = my_bid + cost_half
                entry_i = i
                quoting = False
                continue

            # Sell fill: ask rose through our ask
            if ask[i] > my_ask and ask[i-1] <= my_ask:
                pos = -1
                entry_s = my_ask - cost_half
                entry_i = i
                quoting = False
                continue

        # Exit fills (position != 0, passive mode)
        if pos != 0 and quoting and ts[i] >= q_live_ts and exit_mode == "passive":
            if pos == 1:
                # Sell to close: ask rose through our exit ask
                my_exit = exit_ask_long[i-1]
                if ask[i] > my_exit and ask[i-1] <= my_exit:
                    pnl = ((my_exit - entry_s) - cost_half) / PRICE_SCALE
                    total_pnl += pnl; n_rt += 1
                    if pnl > 0: n_wins += 1
                    daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                    pos = 0; quoting = False
                    continue
            elif pos == -1:
                # Buy to close: bid dropped through our exit bid
                my_exit = exit_bid_short[i-1]
                if bid[i] < my_exit and bid[i-1] >= my_exit:
                    pnl = ((entry_s - my_exit) - cost_half) / PRICE_SCALE
                    total_pnl += pnl; n_rt += 1
                    if pnl > 0: n_wins += 1
                    daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                    pos = 0; quoting = False
                    continue

        # Start quoting when spread is wide
        if wide[i] and not quoting:
            entry_i = i  # remember which tick's quotes to use
            q_live_ts = ts[i] + lat_ns
            quoting = True
        elif not wide[i]:
            quoting = False

    # End close
    if pos != 0:
        pnl = (((bid[-1] + ask[-1]) // 2 - entry_s) * pos - cost_half) / PRICE_SCALE
        total_pnl += pnl; n_rt += 1

    dpnl = np.array(list(daily_pnl.values())) if daily_pnl else np.array([0.0])
    nd = len(daily_pnl)
    sr = float(np.mean(dpnl) / np.std(dpnl)) * np.sqrt(252) if nd >= 2 and np.std(dpnl) > 0 else 0.0
    cum = np.cumsum(dpnl)
    dd = float(np.min(cum - np.maximum.accumulate(cum))) if len(cum) > 0 else 0.0

    return {
        "pnl": round(total_pnl, 1),
        "ntd": round(total_pnl * 10, 0),
        "rt": n_rt,
        "rtd": round(n_rt / max(1, nd), 1),
        "wr": round(n_wins / max(1, n_rt), 3),
        "sl%": round(n_stops / max(1, n_rt) * 100, 1),
        "cx%": round(n_crosses / max(1, n_rt) * 100, 1),
        "sr": round(sr, 2),
        "dd": round(dd, 1),
        "wd": int(np.sum(dpnl > 0)),
        "nd": nd,
    }


def main():
    print("Loading & precomputing...")
    d = load_data()
    pc = precompute(d)
    print(f"  {d['n']:,} ticks, precomputation done")

    from itertools import product as iprod

    thresholds = [3.0, 5.0, 7.0, 10.0, 15.0]
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
    print(f"Running {len(configs)} configs...")

    results = []
    for idx, (thr, sl, skd, (em, et)) in enumerate(configs):
        if idx % 50 == 0:
            print(f"  {idx}/{len(configs)}...")
        r = run_config(pc, thr, sl, skd, em, et)
        r["p"] = {"thr": thr, "sl": sl, "skew": skd, "exit": em, "et": et}
        results.append(r)

    results.sort(key=lambda x: x["pnl"], reverse=True)

    print(f"\n{'='*115}")
    print(f"TOP 30 (of {len(configs)})")
    print(f"{'='*115}")
    hdr = f"{'#':>3} {'PnL':>8} {'NTD':>8} {'SR':>6} {'RT/d':>5} {'Win':>5} {'SL%':>4} {'Cx%':>4} {'DD':>7} {'W/L':>5} | {'thr':>4} {'sl':>3} {'skew':>4} {'exit':>5} {'et':>4}"
    print(hdr)
    for i, r in enumerate(results[:30], 1):
        p = r["p"]
        print(f"{i:>3} {r['pnl']:>+8.0f} {r['ntd']:>+8.0f} {r['sr']:>6.2f} {r['rtd']:>5.0f} {r['wr']:>5.0%} {r['sl%']:>4.0f} {r['cx%']:>4.0f} {r['dd']:>7.0f} {r['wd']:>2}/{r['nd']:>2} | {p['thr']:>4.0f} {p['sl']:>3.0f} {p['skew']:>4} {p['exit'][:4]:>5} {p['et']:>4}")

    profitable = [r for r in results if r["pnl"] > 0]
    print(f"\nProfitable: {len(profitable)}/{len(configs)} ({len(profitable)/len(configs)*100:.1f}%)")

    if profitable:
        from collections import Counter
        print("\nWinning param distribution:")
        for key in ["thr", "sl", "skew", "exit"]:
            vals = [r["p"][key] for r in profitable]
            c = Counter(vals)
            top3 = c.most_common(3)
            print(f"  {key:>5}: {top3}")

    # Also print all losing summary
    all_neg = [r for r in results if r["pnl"] <= 0]
    print(f"\nAll losing: {len(all_neg)}/{len(configs)}")
    if all_neg:
        worst = all_neg[-1]
        print(f"  Worst: {worst['pnl']:+.0f} pts | {worst['p']}")

    out = Path("research/experiments/validations/tmfd6_opmm/sweep_vec_results.json")
    with open(out, "w") as f:
        json.dump(results[:100], f, indent=2)
    print(f"\nSaved top 100 to {out}")


if __name__ == "__main__":
    main()
