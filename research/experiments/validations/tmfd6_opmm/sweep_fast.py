"""Focused TMFD6 OpMM parameter sweep — optimized for speed.

Tests the most impactful dimensions based on v3 diagnosis:
1. Inventory skew (the #1 killer) — 4 levels
2. Exit mode (passive vs aggressive vs immediate) — 4 levels
3. Stop-loss (tradeoff) — 4 levels
4. Spread threshold — 3 levels

Total: 4×4×4×3 = 192 configs (vs 2880 in full sweep).
Queue depth filter fixed at 999 (disabled) — it rejected too many fills.
Imbalance filter tested as separate pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import numpy as np

PRICE_SCALE = 10000
IMBALANCE_COEFF_PERCENT = 20
TICK_SIZE_RATIO_PCT = 50


def _compute_quotes(mid_x2: int, spread_s: int, imbalance: float, position: int,
                    skew_divisor: int) -> tuple[int, int]:
    imbalance_adj = int(imbalance * spread_s * IMBALANCE_COEFF_PERCENT * 2 // 100)
    micro_x2 = mid_x2 + imbalance_adj
    tick_s = max(1, spread_s * TICK_SIZE_RATIO_PCT // 100)
    skew_x2 = -(position * tick_s * 2) // skew_divisor if skew_divisor < 999 else 0
    fv_x2 = micro_x2 + skew_x2
    half_sp = max(1, spread_s // 2)
    qw = max(tick_s, half_sp)
    return (fv_x2 - qw * 2) // 2, (fv_x2 + qw * 2) // 2


def run_one(thr_bps: float, stop_pts: float, skew_div: int,
            exit_mode: str, exit_ticks: int,
            bid_s: np.ndarray, ask_s: np.ndarray, bq: np.ndarray, aq: np.ndarray,
            ts: np.ndarray) -> dict:
    n = len(bid_s)
    cost_half = int(2.0 * PRICE_SCALE)
    stop_s = int(stop_pts * PRICE_SCALE) if stop_pts > 0 else 0
    lat_submit = 36_000_000
    lat_cancel = 47_000_000

    pos = 0
    entry_s = 0
    entry_i = 0
    qb = qa = 0
    q_live = 0
    q_on = False
    q_canc = False
    q_canc_ts = 0

    total_pnl = 0.0
    n_rt = 0
    n_stops = 0
    n_wins = 0
    n_crosses = 0
    daily_pnl: dict[str, float] = {}
    GAP = 4 * 3600 * 10**9
    cur_day = ""

    for i in range(1, n):
        cb, ca, ct = bid_s[i], ask_s[i], ts[i]
        sp = ca - cb
        mx2 = cb + ca
        sp_bps = sp / (mx2 / 2.0) * 10000.0 if mx2 > 0 else 0.0

        # Day boundary
        if ct - ts[i-1] > GAP or i == 1:
            if pos != 0:
                pnl = ((mx2 // 2 - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1
                daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                pos = 0
            q_on = False; q_canc = False
            dt = datetime.fromtimestamp(ct / 1e9, tz=timezone.utc)
            cur_day = dt.strftime("%Y-%m-%d")
            daily_pnl.setdefault(cur_day, 0.0)
            continue

        # Stop-loss (exit at adverse side)
        if pos != 0 and stop_s > 0:
            unreal = (mx2 // 2 - entry_s) * pos
            if unreal < -stop_s:
                exit_p = cb if pos > 0 else ca
                pnl = ((exit_p - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1; n_stops += 1
                daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                pos = 0; q_on = False; q_canc = False
                continue

        # Aggressive/immediate exit
        if pos != 0:
            do_cross = False
            if exit_mode == "immediate":
                do_cross = True
            elif exit_mode == "aggressive" and (i - entry_i) >= exit_ticks:
                do_cross = True
            if do_cross:
                exit_p = cb if pos > 0 else ca
                pnl = ((exit_p - entry_s) * pos - cost_half) / PRICE_SCALE
                total_pnl += pnl; n_rt += 1; n_crosses += 1
                if pnl > 0: n_wins += 1
                daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                pos = 0; q_on = False; q_canc = False
                continue

        # Cancel completion
        if q_canc and ct >= q_canc_ts:
            q_on = False; q_canc = False

        # Fill detection
        if q_on and not q_canc and ct >= q_live:
            pb, pa = bid_s[i-1], ask_s[i-1]
            buy_t = cb < qb and pb >= qb
            sell_t = ca > qa and pa <= qa

            if buy_t and pos <= 0:
                if pos == -1:
                    pnl = ((entry_s - qb) - cost_half) / PRICE_SCALE
                    total_pnl += pnl; n_rt += 1
                    if pnl > 0: n_wins += 1
                    daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                    pos = 0
                else:
                    pos = 1; entry_s = qb + cost_half; entry_i = i
                q_on = False; continue

            if sell_t and pos >= 0:
                if pos == 1:
                    pnl = ((qa - entry_s) - cost_half) / PRICE_SCALE
                    total_pnl += pnl; n_rt += 1
                    if pnl > 0: n_wins += 1
                    daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                    pos = 0
                else:
                    pos = -1; entry_s = qa - cost_half; entry_i = i
                q_on = False; continue

        # Quote generation
        if sp_bps >= thr_bps and mx2 > 0 and sp > 0:
            tq = bq[i] + aq[i]
            imb = (bq[i] - aq[i]) / tq if tq > 0 else 0.0
            nb, na = _compute_quotes(mx2, sp, imb, pos, skew_div)
            if nb > 0 and na > nb:
                if not q_on or nb != qb or na != qa:
                    qb, qa = nb, na
                    q_live = ct + lat_submit
                    q_on = True; q_canc = False
        elif q_on and not q_canc:
            q_canc = True; q_canc_ts = ct + lat_cancel

    if pos != 0:
        pnl = (((bid_s[-1] + ask_s[-1]) // 2 - entry_s) * pos - cost_half) / PRICE_SCALE
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
    print("Loading data...")
    data = np.load("research/data/raw/tmfd6/TMFD6_all_l1.npy")
    bs = (data["bid_px"] * PRICE_SCALE).astype(np.int64)
    aks = (data["ask_px"] * PRICE_SCALE).astype(np.int64)
    bq = data["bid_qty"]
    aq = data["ask_qty"]
    ts = data["local_ts"]
    print(f"  {len(data):,} ticks loaded")

    # Sweep grid
    thresholds = [5.0, 7.0, 10.0]
    stop_losses = [0, 10, 20, 50]
    skew_divs = [5, 20, 50, 999]
    exits = [
        ("passive", 0),
        ("aggressive", 30),
        ("aggressive", 100),
        ("immediate", 0),
    ]

    configs = list(product(thresholds, stop_losses, skew_divs, exits))
    print(f"Running {len(configs)} configs...")

    results = []
    for idx, (thr, sl, skd, (em, et)) in enumerate(configs):
        if idx % 48 == 0:
            print(f"  {idx}/{len(configs)}...")
        r = run_one(thr, sl, skd, em, et, bs, aks, bq, aq, ts)
        r["p"] = {"thr": thr, "sl": sl, "skew": skd, "exit": em, "et": et}
        results.append(r)

    results.sort(key=lambda x: x["pnl"], reverse=True)

    print(f"\n{'='*110}")
    print(f"TOP 30 (of {len(configs)})")
    print(f"{'='*110}")
    print(f"{'#':>3} {'PnL':>8} {'NTD':>8} {'SR':>6} {'RT/d':>5} {'Win':>4} {'SL%':>4} {'Cx%':>4} {'DD':>7} {'W/L':>4} | {'thr':>4} {'sl':>3} {'skew':>4} {'exit':>5} {'et':>4}")
    for i, r in enumerate(results[:30], 1):
        p = r["p"]
        print(f"{i:>3} {r['pnl']:>+8.0f} {r['ntd']:>+8.0f} {r['sr']:>6.2f} {r['rtd']:>5.0f} {r['wr']:>4.0%} {r['sl%']:>4.0f} {r['cx%']:>4.0f} {r['dd']:>7.0f} {r['wd']:>2}/{r['nd']:>1} | {p['thr']:>4.0f} {p['sl']:>3.0f} {p['skew']:>4} {p['exit'][:4]:>5} {p['et']:>4}")

    # Analysis: what matters most?
    profitable = [r for r in results if r["pnl"] > 0]
    print(f"\nProfitable: {len(profitable)}/{len(configs)} ({len(profitable)/len(configs)*100:.1f}%)")

    if profitable:
        from collections import Counter
        print("\nWinning parameter distribution:")
        for key in ["thr", "sl", "skew", "exit"]:
            vals = [r["p"][key] for r in profitable]
            c = Counter(vals)
            print(f"  {key:>5}: {dict(c.most_common())}")

    # Save
    out = Path("research/experiments/validations/tmfd6_opmm/sweep_fast_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
