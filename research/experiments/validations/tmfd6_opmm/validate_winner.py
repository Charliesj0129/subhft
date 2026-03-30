"""Validate sweep winner: thr=7bps, skew=999, stop=0, passive.

Outputs:
- Daily PnL breakdown
- Intraday max unrealized drawdown per day
- Single-day concentration check (Challenger condition: no day > 40%)
- Full trade log for analysis
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PRICE_SCALE = 10000


def compute_quotes(mid_x2, spread, imbalance):
    """Entry quotes at position=0 with skew=999 (effectively zero)."""
    imb_adj = int(imbalance * spread * 20 * 2 // 100)
    micro_x2 = mid_x2 + imb_adj
    tick_s = max(1, int(spread * 50 // 100))
    half_sp = max(1, int(spread // 2))
    qw = max(tick_s, half_sp)
    return (micro_x2 - qw * 2) // 2, (micro_x2 + qw * 2) // 2


def main():
    print("Loading data...", flush=True)
    data = np.load("research/data/raw/tmfd6/TMFD6_all_l1.npy")
    bid = (data["bid_px"] * PRICE_SCALE).astype(np.int64)
    ask = (data["ask_px"] * PRICE_SCALE).astype(np.int64)
    bq, aq = data["bid_qty"], data["ask_qty"]
    ts = data["local_ts"]
    n = len(data)

    THR_BPS = 7.0
    COST_HALF = int(2.0 * PRICE_SCALE)
    LAT = 36_000_000

    pos = 0
    entry_s = 0
    my_bid = my_ask = 0
    q_live = 0
    quoting = False

    GAP = 4 * 3600 * 10**9
    cur_day = ""

    # Per-day tracking
    daily_pnl: dict[str, float] = {}
    daily_trades: dict[str, int] = {}
    daily_max_unreal_dd: dict[str, float] = {}  # worst unrealized loss per day
    intraday_peak_unreal = 0.0  # running peak unrealized PnL within day
    intraday_unreal = 0.0
    intraday_max_dd = 0.0

    # Trade log
    trades: list[dict] = []

    # Price change mask
    bid_ch = np.zeros(n, dtype=bool)
    ask_ch = np.zeros(n, dtype=bool)
    bid_ch[1:] = bid[1:] != bid[:-1]
    ask_ch[1:] = ask[1:] != ask[:-1]
    px_changed = bid_ch | ask_ch

    print(f"  {n:,} ticks, {int(px_changed.sum()):,} price changes", flush=True)

    for i in range(1, n):
        cb, ca, ct = bid[i], ask[i], ts[i]
        sp = ca - cb
        mx2 = cb + ca
        sp_bps = sp / (mx2 / 2.0) * 10000.0 if mx2 > 0 else 0.0

        # Day boundary
        if ct - ts[i - 1] > GAP or i == 1:
            # Save intraday DD for previous day
            if cur_day and cur_day in daily_pnl:
                daily_max_unreal_dd[cur_day] = round(intraday_max_dd, 2)

            if pos != 0:
                pnl = ((mx2 // 2 - entry_s) * pos - COST_HALF) / PRICE_SCALE
                daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                daily_trades[cur_day] = daily_trades.get(cur_day, 0) + 1
                trades.append({"day": cur_day, "pnl": round(pnl, 2), "type": "day_close"})
                pos = 0

            quoting = False
            dt_obj = datetime.fromtimestamp(ct / 1e9, tz=timezone.utc)
            cur_day = dt_obj.strftime("%Y-%m-%d")
            daily_pnl.setdefault(cur_day, 0.0)
            daily_trades.setdefault(cur_day, 0)
            intraday_peak_unreal = 0.0
            intraday_unreal = 0.0
            intraday_max_dd = 0.0
            continue

        # Track intraday unrealized PnL
        if pos != 0:
            mid_s = mx2 // 2
            intraday_unreal = ((mid_s - entry_s) * pos) / PRICE_SCALE
            if intraday_unreal > intraday_peak_unreal:
                intraday_peak_unreal = intraday_unreal
            dd = intraday_unreal - intraday_peak_unreal
            if dd < intraday_max_dd:
                intraday_max_dd = dd

        # Fill detection (only on price changes)
        if quoting and ct >= q_live and px_changed[i]:
            if pos == 0:
                if bid[i] < my_bid and bid[i - 1] >= my_bid:
                    pos = 1
                    entry_s = my_bid + COST_HALF
                    quoting = False
                    intraday_peak_unreal = 0.0
                    continue
                if ask[i] > my_ask and ask[i - 1] <= my_ask:
                    pos = -1
                    entry_s = my_ask - COST_HALF
                    quoting = False
                    intraday_peak_unreal = 0.0
                    continue
            else:
                # Exit: use same quotes (skew=999 → symmetric)
                if pos == 1 and ask[i] > my_ask and ask[i - 1] <= my_ask:
                    pnl = ((my_ask - entry_s) - COST_HALF) / PRICE_SCALE
                    daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                    daily_trades[cur_day] = daily_trades.get(cur_day, 0) + 1
                    trades.append({"day": cur_day, "pnl": round(pnl, 2), "type": "sell_close"})
                    pos = 0
                    quoting = False
                    intraday_peak_unreal = 0.0
                    continue
                if pos == -1 and bid[i] < my_bid and bid[i - 1] >= my_bid:
                    pnl = ((entry_s - my_bid) - COST_HALF) / PRICE_SCALE
                    daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
                    daily_trades[cur_day] = daily_trades.get(cur_day, 0) + 1
                    trades.append({"day": cur_day, "pnl": round(pnl, 2), "type": "buy_close"})
                    pos = 0
                    quoting = False
                    intraday_peak_unreal = 0.0
                    continue

        # Quote when wide
        wide = sp_bps >= THR_BPS
        if wide and not quoting:
            tq = bq[i] + aq[i]
            imb = (bq[i] - aq[i]) / tq if tq > 0 else 0.0
            my_bid, my_ask = compute_quotes(mx2, sp, imb)
            if my_bid > 0 and my_ask > my_bid:
                q_live = ct + LAT
                quoting = True
        elif not wide and quoting and pos == 0:
            quoting = False

    # Final day DD
    if cur_day:
        daily_max_unreal_dd[cur_day] = round(intraday_max_dd, 2)

    if pos != 0:
        pnl = (((bid[-1] + ask[-1]) // 2 - entry_s) * pos - COST_HALF) / PRICE_SCALE
        daily_pnl[cur_day] = daily_pnl.get(cur_day, 0) + pnl
        daily_trades[cur_day] = daily_trades.get(cur_day, 0) + 1

    # Results
    total = sum(daily_pnl.values())
    dpnl = np.array(list(daily_pnl.values()))
    nd = len(dpnl)
    sr = float(np.mean(dpnl) / np.std(dpnl)) * np.sqrt(252) if nd >= 2 and np.std(dpnl) > 0 else 0.0
    cum = np.cumsum(dpnl)
    dd = float(np.min(cum - np.maximum.accumulate(cum)))

    print(f"\n{'='*70}")
    print(f"WINNER VALIDATION: thr=7bps, skew=OFF, stop=OFF, passive")
    print(f"{'='*70}")
    print(f"Total PnL: {total:+.0f} pts ({total*10:+.0f} NTD)")
    print(f"Sharpe: {sr:.2f} | Max DD: {dd:.0f} pts")
    print(f"Days: {nd} | RTs: {sum(daily_trades.values())}")

    print(f"\n{'Day':<12} {'PnL':>8} {'NTD':>8} {'RTs':>5} {'MaxUnrealDD':>12} {'%Total':>7}")
    sorted_days = sorted(daily_pnl.keys())
    for day in sorted_days:
        pnl = daily_pnl[day]
        tr = daily_trades.get(day, 0)
        udd = daily_max_unreal_dd.get(day, 0)
        pct = pnl / total * 100 if total != 0 else 0
        flag = " *** >40%" if abs(pct) > 40 else ""
        print(f"{day:<12} {pnl:>+8.1f} {pnl*10:>+8.0f} {tr:>5} {udd:>+12.1f} {pct:>6.1f}%{flag}")

    # Challenger condition checks
    print(f"\n--- Challenger Conditions ---")
    max_day_pct = max(abs(pnl / total * 100) for pnl in daily_pnl.values()) if total != 0 else 0
    max_intraday_dd = min(daily_max_unreal_dd.values()) if daily_max_unreal_dd else 0
    print(f"1. Max single-day concentration: {max_day_pct:.1f}% {'PASS (<40%)' if max_day_pct < 40 else 'FAIL (>40%)'}")
    print(f"2. Worst intraday unrealized DD: {max_intraday_dd:.1f} pts {'PASS (>-50)' if max_intraday_dd > -50 else 'WARN (<-50)'}")

    # Save
    results = {
        "config": {"thr_bps": 7.0, "skew": 999, "stop": 0, "exit": "passive"},
        "total_pnl_pts": round(total, 2),
        "total_pnl_ntd": round(total * 10, 2),
        "sharpe": round(sr, 2),
        "max_dd": round(dd, 2),
        "n_days": nd,
        "daily": {d: {"pnl": round(daily_pnl[d], 2), "trades": daily_trades.get(d, 0),
                       "max_unreal_dd": daily_max_unreal_dd.get(d, 0),
                       "pct_of_total": round(daily_pnl[d] / total * 100, 1) if total != 0 else 0}
                  for d in sorted_days},
        "challenger_check": {
            "max_day_concentration_pct": round(max_day_pct, 1),
            "worst_intraday_unreal_dd_pts": round(max_intraday_dd, 1),
            "pass_concentration": bool(max_day_pct < 40),
            "pass_intraday_dd": bool(max_intraday_dd > -50),
        },
    }
    out = Path("research/experiments/validations/tmfd6_opmm/winner_validation.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
