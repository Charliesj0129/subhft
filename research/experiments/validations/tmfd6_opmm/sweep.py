"""Exhaustive TMFD6 OpMM parameter sweep.

Tests every combination of:
- Spread threshold (3-20 bps)
- Stop-loss (5, 10, 15, 20, 50, disabled)
- Inventory skew divisor (5=production, 10, 20, 50, 999=disabled)
- Queue depth filter (4, 8, 12, 999=disabled)
- Exit mode: passive (wait for other-side fill), aggressive (cross spread after N ticks),
  immediate (take spread capture immediately)
- Imbalance filter: disabled, favorable-only (only enter when imbalance favors us)

Uses production-parity quoting (scaled integers, exact SimpleMarketMaker formula).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import numpy as np

PRICE_SCALE = 10000
IMBALANCE_COEFF_PERCENT = 20
TICK_SIZE_RATIO_PCT = 50


@dataclass
class Params:
    spread_threshold_bps: float = 5.0
    stop_loss_pts: float = 20.0  # 0 = disabled
    skew_divisor: int = 5  # higher = less skew
    queue_depth_max: float = 8.0  # 999 = disabled
    exit_mode: str = "passive"  # passive | aggressive_N | immediate
    exit_cross_ticks: int = 200  # for aggressive mode: cross spread after N ticks
    imbalance_filter: bool = False  # True = only enter when imbalance favors our side
    rt_cost_pts: float = 4.0
    submit_latency_ns: int = 36_000_000
    cancel_latency_ns: int = 47_000_000


def _compute_quotes(mid_x2: int, spread_s: int, imbalance: float, position: int,
                    skew_divisor: int) -> tuple[int, int]:
    imbalance_adj = int(imbalance * spread_s * IMBALANCE_COEFF_PERCENT * 2 // 100)
    micro_x2 = mid_x2 + imbalance_adj
    tick_s = max(1, spread_s * TICK_SIZE_RATIO_PCT // 100)
    skew_x2 = -(position * tick_s * 2) // skew_divisor if skew_divisor < 999 else 0
    fv_x2 = micro_x2 + skew_x2
    half_spread_s = max(1, spread_s // 2)
    qw_s = max(tick_s, half_spread_s)
    bid_s = (fv_x2 - qw_s * 2) // 2
    ask_s = (fv_x2 + qw_s * 2) // 2
    return bid_s, ask_s


def run_one(p: Params, bid_s: np.ndarray, ask_s: np.ndarray, bid_qty: np.ndarray,
            ask_qty: np.ndarray, ts: np.ndarray) -> dict:
    n = len(bid_s)
    cost_half_s = int(p.rt_cost_pts / 2.0 * PRICE_SCALE)
    stop_s = int(p.stop_loss_pts * PRICE_SCALE) if p.stop_loss_pts > 0 else 0

    position = 0
    entry_price_s = 0
    entry_tick = 0
    q_bid_s = q_ask_s = 0
    q_live_ts = 0
    q_active = False
    q_cancel_pending = False
    q_cancel_ts = 0

    total_pnl = 0.0
    n_trades = 0
    n_stops = 0
    n_wins = 0
    n_crosses = 0  # aggressive exit crosses
    pnl_list: list[float] = []
    daily_pnl: dict[str, float] = {}
    daily_trades: dict[str, int] = {}

    DAY_GAP = 4 * 3600 * 1_000_000_000
    current_day = ""

    for i in range(1, n):
        cb = bid_s[i]
        ca = ask_s[i]
        ct = ts[i]
        sp = ca - cb
        mx2 = cb + ca

        sp_bps = sp / (mx2 / 2.0) * 10000.0 if mx2 > 0 else 0.0

        # Day boundary
        if ct - ts[i - 1] > DAY_GAP or i == 1:
            if position != 0:
                mid = mx2 // 2
                pnl_s = (mid - entry_price_s) * position - cost_half_s
                pnl = pnl_s / PRICE_SCALE
                total_pnl += pnl
                pnl_list.append(pnl)
                n_trades += 1
                if current_day:
                    daily_pnl[current_day] = daily_pnl.get(current_day, 0) + pnl
                    daily_trades[current_day] = daily_trades.get(current_day, 0) + 1
                position = 0
            q_active = False
            q_cancel_pending = False
            dt_obj = datetime.fromtimestamp(ct / 1e9, tz=timezone.utc)
            current_day = dt_obj.strftime("%Y-%m-%d")
            daily_pnl.setdefault(current_day, 0.0)
            daily_trades.setdefault(current_day, 0)
            continue

        # Stop-loss
        if position != 0 and stop_s > 0:
            mid = mx2 // 2
            unreal = (mid - entry_price_s) * position
            if unreal < -stop_s:
                exit_s = cb if position > 0 else ca
                pnl_s = (exit_s - entry_price_s) * position - cost_half_s
                pnl = pnl_s / PRICE_SCALE
                total_pnl += pnl
                pnl_list.append(pnl)
                n_trades += 1
                n_stops += 1
                daily_pnl[current_day] = daily_pnl.get(current_day, 0) + pnl
                daily_trades[current_day] = daily_trades.get(current_day, 0) + 1
                position = 0
                q_active = False
                q_cancel_pending = False
                continue

        # Aggressive/immediate exit: cross spread after N ticks or immediately
        if position != 0:
            ticks_held = i - entry_tick
            do_cross = False
            if p.exit_mode == "immediate":
                do_cross = True
            elif p.exit_mode.startswith("aggressive") and ticks_held >= p.exit_cross_ticks:
                do_cross = True

            if do_cross:
                if position > 0:
                    exit_s = cb  # sell at bid (cross spread)
                else:
                    exit_s = ca  # buy at ask (cross spread)
                pnl_s = (exit_s - entry_price_s) * position - cost_half_s
                pnl = pnl_s / PRICE_SCALE
                total_pnl += pnl
                pnl_list.append(pnl)
                n_trades += 1
                n_crosses += 1
                if pnl > 0:
                    n_wins += 1
                daily_pnl[current_day] = daily_pnl.get(current_day, 0) + pnl
                daily_trades[current_day] = daily_trades.get(current_day, 0) + 1
                position = 0
                q_active = False
                q_cancel_pending = False
                continue

        # Cancel completion
        if q_cancel_pending and ct >= q_cancel_ts:
            q_active = False
            q_cancel_pending = False

        # Fill detection
        if q_active and not q_cancel_pending and ct >= q_live_ts:
            pb = bid_s[i - 1]
            pa = ask_s[i - 1]
            buy_trig = (cb < q_bid_s and pb >= q_bid_s)
            sell_trig = (ca > q_ask_s and pa <= q_ask_s)

            # Queue depth filter
            if buy_trig and bid_qty[i - 1] > p.queue_depth_max:
                buy_trig = False
            if sell_trig and ask_qty[i - 1] > p.queue_depth_max:
                sell_trig = False

            # Imbalance filter: only enter on favorable side
            if p.imbalance_filter:
                total_q = bid_qty[i] + ask_qty[i]
                if total_q > 0:
                    imb = (bid_qty[i] - ask_qty[i]) / total_q
                else:
                    imb = 0.0
                # Buy only if bid-heavy (imb > 0), sell only if ask-heavy (imb < 0)
                if buy_trig and position == 0 and imb < 0.1:
                    buy_trig = False
                if sell_trig and position == 0 and imb > -0.1:
                    sell_trig = False

            if buy_trig and position <= 0:
                fill_s = q_bid_s
                if position == -1:
                    pnl_s = (entry_price_s - fill_s) - cost_half_s
                    pnl = pnl_s / PRICE_SCALE
                    total_pnl += pnl
                    pnl_list.append(pnl)
                    n_trades += 1
                    if pnl > 0:
                        n_wins += 1
                    daily_pnl[current_day] = daily_pnl.get(current_day, 0) + pnl
                    daily_trades[current_day] = daily_trades.get(current_day, 0) + 1
                    position = 0
                else:
                    position = 1
                    entry_price_s = fill_s + cost_half_s
                    entry_tick = i
                q_active = False
                continue

            if sell_trig and position >= 0:
                fill_s = q_ask_s
                if position == 1:
                    pnl_s = (fill_s - entry_price_s) - cost_half_s
                    pnl = pnl_s / PRICE_SCALE
                    total_pnl += pnl
                    pnl_list.append(pnl)
                    n_trades += 1
                    if pnl > 0:
                        n_wins += 1
                    daily_pnl[current_day] = daily_pnl.get(current_day, 0) + pnl
                    daily_trades[current_day] = daily_trades.get(current_day, 0) + 1
                    position = 0
                else:
                    position = -1
                    entry_price_s = fill_s - cost_half_s
                    entry_tick = i
                q_active = False
                continue

        # Quote generation
        if sp_bps >= p.spread_threshold_bps and mx2 > 0 and sp > 0 and position == 0:
            total_q = bid_qty[i] + ask_qty[i]
            imb = (bid_qty[i] - ask_qty[i]) / total_q if total_q > 0 else 0.0
            qb, qa = _compute_quotes(mx2, sp, imb, position, p.skew_divisor)
            if qb > 0 and qa > qb:
                q_bid_s, q_ask_s = qb, qa
                q_live_ts = ct + p.submit_latency_ns
                q_active = True
                q_cancel_pending = False
        elif position != 0 and sp_bps >= p.spread_threshold_bps and p.exit_mode == "passive":
            # Exit quote
            total_q = bid_qty[i] + ask_qty[i]
            imb = (bid_qty[i] - ask_qty[i]) / total_q if total_q > 0 else 0.0
            qb, qa = _compute_quotes(mx2, sp, imb, position, p.skew_divisor)
            if qb > 0 and qa > qb:
                q_bid_s, q_ask_s = qb, qa
                q_live_ts = ct + p.submit_latency_ns
                q_active = True
                q_cancel_pending = False
        elif q_active and not q_cancel_pending:
            q_cancel_pending = True
            q_cancel_ts = ct + p.cancel_latency_ns

    # End close
    if position != 0:
        mid = (bid_s[-1] + ask_s[-1]) // 2
        pnl_s = (mid - entry_price_s) * position - cost_half_s
        pnl = pnl_s / PRICE_SCALE
        total_pnl += pnl
        pnl_list.append(pnl)
        n_trades += 1

    pnls = np.array(pnl_list) if pnl_list else np.array([0.0])
    dpnl = np.array(list(daily_pnl.values())) if daily_pnl else np.array([0.0])
    n_days = len(daily_pnl)

    sharpe = 0.0
    if n_days >= 2 and np.std(dpnl) > 0:
        sharpe = float(np.mean(dpnl) / np.std(dpnl)) * np.sqrt(252)

    cum = np.cumsum(dpnl)
    peak = np.maximum.accumulate(cum)
    max_dd = float(np.min(cum - peak)) if len(cum) > 0 else 0.0

    return {
        "pnl_pts": round(total_pnl, 1),
        "pnl_ntd": round(total_pnl * 10, 0),
        "n_trades": n_trades,
        "rts_day": round(n_trades / max(1, n_days), 1),
        "win_rate": round(n_wins / max(1, n_trades), 3),
        "stop_pct": round(n_stops / max(1, n_trades) * 100, 1),
        "cross_pct": round(n_crosses / max(1, n_trades) * 100, 1),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 1),
        "mean_rt": round(float(np.mean(pnls)), 2),
        "win_days": int(np.sum(dpnl > 0)),
        "n_days": n_days,
    }


def main():
    data_path = "research/data/raw/tmfd6/TMFD6_all_l1.npy"
    print(f"Loading {data_path}...")
    data = np.load(data_path)
    bid_s = (data["bid_px"] * PRICE_SCALE).astype(np.int64)
    ask_s = (data["ask_px"] * PRICE_SCALE).astype(np.int64)
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    ts_arr = data["local_ts"]
    print(f"Loaded {len(data):,} ticks")

    # Define sweep grid
    thresholds = [5.0, 7.0, 10.0, 15.0, 20.0]
    stop_losses = [0, 5, 10, 15, 20, 50]  # 0 = disabled
    skew_divs = [5, 10, 20, 999]  # 999 = no skew
    queue_maxes = [4, 8, 999]  # 999 = no filter
    exit_modes = [
        ("passive", 0),
        ("aggressive", 50),   # cross after 50 ticks (~6s)
        ("aggressive", 200),  # cross after 200 ticks (~25s)
        ("immediate", 0),     # cross immediately
    ]
    imb_filters = [False, True]

    configs = []
    for thr, sl, skd, qm, (em, ec), imf in product(
        thresholds, stop_losses, skew_divs, queue_maxes, exit_modes, imb_filters
    ):
        configs.append(Params(
            spread_threshold_bps=thr, stop_loss_pts=sl, skew_divisor=skd,
            queue_depth_max=qm, exit_mode=em, exit_cross_ticks=ec,
            imbalance_filter=imf,
        ))

    print(f"Running {len(configs)} configurations...")

    results = []
    for idx, p in enumerate(configs):
        if idx % 100 == 0:
            print(f"  {idx}/{len(configs)}...")
        r = run_one(p, bid_s, ask_s, bid_qty, ask_qty, ts_arr)
        r["params"] = {
            "thr": p.spread_threshold_bps, "sl": p.stop_loss_pts, "skew": p.skew_divisor,
            "qdepth": p.queue_depth_max, "exit": p.exit_mode, "exit_ticks": p.exit_cross_ticks,
            "imb": p.imbalance_filter,
        }
        results.append(r)

    # Sort by PnL descending
    results.sort(key=lambda x: x["pnl_pts"], reverse=True)

    # Print top 20 and bottom 5
    print(f"\n{'='*120}")
    print(f"TOP 20 CONFIGURATIONS (of {len(results)} total)")
    print(f"{'='*120}")
    hdr = f"{'#':>3} {'PnL':>8} {'NTD':>9} {'SR':>6} {'RT/d':>5} {'Win':>5} {'SL%':>4} {'Cx%':>4} {'DD':>7} {'mean':>6} {'W/L':>5} | {'thr':>4} {'sl':>3} {'skew':>4} {'qd':>4} {'exit':>6} {'et':>4} {'imb':>3}"
    print(hdr)
    for rank, r in enumerate(results[:20], 1):
        p = r["params"]
        em_short = p["exit"][:4]
        print(f"{rank:>3} {r['pnl_pts']:>+8.0f} {r['pnl_ntd']:>+9.0f} {r['sharpe']:>6.2f} {r['rts_day']:>5.0f} {r['win_rate']:>5.0%} {r['stop_pct']:>4.0f} {r['cross_pct']:>4.0f} {r['max_dd']:>7.0f} {r['mean_rt']:>+6.1f} {r['win_days']:>2}/{r['n_days']:>2} | {p['thr']:>4.0f} {p['sl']:>3.0f} {p['skew']:>4} {p['qd']:>4.0f} {em_short:>6} {p['exit_ticks']:>4} {str(p['imb'])[0]:>3}")

    print(f"\n{'='*120}")
    print("BOTTOM 5")
    for rank, r in enumerate(results[-5:], len(results) - 4):
        p = r["params"]
        em_short = p["exit"][:4]
        print(f"{rank:>3} {r['pnl_pts']:>+8.0f} {r['pnl_ntd']:>+9.0f} {r['sharpe']:>6.2f} {r['rts_day']:>5.0f} {r['win_rate']:>5.0%} {r['stop_pct']:>4.0f} {r['cross_pct']:>4.0f} {r['max_dd']:>7.0f} {r['mean_rt']:>+6.1f} {r['win_days']:>2}/{r['n_days']:>2} | {p['thr']:>4.0f} {p['sl']:>3.0f} {p['skew']:>4} {p['qd']:>4.0f} {em_short:>6} {p['exit_ticks']:>4} {str(p['imb'])[0]:>3}")

    # Stats
    profitable = [r for r in results if r["pnl_pts"] > 0]
    print(f"\nProfitable: {len(profitable)}/{len(results)} ({len(profitable)/len(results)*100:.1f}%)")

    # Common traits of top configs
    if len(profitable) >= 5:
        print("\nCommon traits of top-20 profitable configs:")
        top20 = results[:20]
        for key in ["thr", "sl", "skew", "qdepth", "exit", "imb"]:
            vals = [r["params"][key] for r in top20]
            from collections import Counter
            c = Counter(vals)
            print(f"  {key}: {c.most_common(3)}")

    # Save full results
    out = Path("research/experiments/validations/tmfd6_opmm/sweep_results.json")
    with open(out, "w") as f:
        json.dump(results[:50], f, indent=2)  # top 50 only
    print(f"\nSaved top 50 to {out}")


if __name__ == "__main__":
    main()
