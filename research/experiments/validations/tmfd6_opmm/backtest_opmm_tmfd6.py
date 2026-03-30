"""TMFD6 OpportunisticMM tick-level backtest.

Simulates symmetric quoting when spread > threshold, with latency-aware fills.
All prices in index points (1 pt = 10 NTD for XMT/TMFD6).

Fill model (conservative maker):
- Bid at P fills when next-tick bid < P (bid level consumed → someone sold through us)
- Ask at P fills when next-tick ask > P (ask level consumed → someone bought through us)
This is conservative: requires price to move THROUGH our level, not just touch it.

Fee model: 40 NTD RT = 4 points (tax 7 + comm 13 per side).
Latency: 36ms Shioaji P95 RTT (order submission delay).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


@dataclass
class Config:
    data_path: str = "research/data/raw/tmfd6/TMFD6_all_l1.npy"
    spread_threshold_pts: int = 5  # minimum spread to quote
    rt_cost_pts: float = 4.0  # round-trip cost in points
    half_cost_pts: float = 2.0  # per-side cost
    latency_ns: int = 36_000_000  # 36ms
    max_hold_ticks: int = 5000  # force close after N ticks if not filled
    output_dir: str = "research/experiments/validations/tmfd6_opmm"


def run_backtest(cfg: Config) -> dict:
    data = np.load(cfg.data_path)
    bid = data["bid_px"]
    ask = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    ts = data["local_ts"]
    n = len(data)

    # State
    position = 0  # -1, 0, +1
    entry_price = 0.0
    entry_tick = 0

    # Quote state
    q_bid = 0.0
    q_ask = 0.0
    q_live_ts = 0  # when quote becomes live
    q_active = False

    # Stats
    trade_pnls: list[float] = []
    trade_spreads: list[float] = []  # spread at entry
    daily_pnl: dict[str, float] = {}
    daily_trades: dict[str, int] = {}
    n_fills_buy = 0
    n_fills_sell = 0
    adverse_count = 0  # mid moved against us 1 tick after fill

    DAY_GAP_NS = 4 * 3600 * 1_000_000_000
    current_day = ""

    for i in range(1, n):
        cb = bid[i]
        ca = ask[i]
        ct = ts[i]
        spread = ca - cb

        # Day boundary
        if ct - ts[i - 1] > DAY_GAP_NS or i == 1:
            # Force close
            if position != 0:
                mid = (cb + ca) / 2.0
                pnl = (mid - entry_price) * position - cfg.half_cost_pts
                trade_pnls.append(pnl)
                if current_day:
                    daily_pnl[current_day] = daily_pnl.get(current_day, 0.0) + pnl
                    daily_trades[current_day] = daily_trades.get(current_day, 0) + 1
                position = 0
            q_active = False
            dt = datetime.fromtimestamp(ct / 1e9, tz=timezone.utc)
            current_day = dt.strftime("%Y-%m-%d")
            daily_pnl.setdefault(current_day, 0.0)
            daily_trades.setdefault(current_day, 0)
            continue

        # Force close if held too long
        if position != 0 and (i - entry_tick) > cfg.max_hold_ticks:
            mid = (cb + ca) / 2.0
            pnl = (mid - entry_price) * position - cfg.half_cost_pts
            trade_pnls.append(pnl)
            if current_day:
                daily_pnl[current_day] += pnl
                daily_trades[current_day] += 1
            position = 0
            q_active = False

        # Fill detection (only when quotes are live)
        if q_active and ct >= q_live_ts:
            prev_b = bid[i - 1]
            prev_a = ask[i - 1]

            # Buy fill: bid dropped through our bid level
            # (Someone sold aggressively, consuming depth at our price)
            if position <= 0 and cb < q_bid and prev_b >= q_bid:
                fill_px = q_bid
                if position == 0:
                    position = 1
                    entry_price = fill_px + cfg.half_cost_pts
                    entry_tick = i
                    trade_spreads.append(spread)
                    n_fills_buy += 1
                    # Adverse check: did mid drop after our buy?
                    if i + 5 < n:
                        future_mid = (bid[i + 5] + ask[i + 5]) / 2.0
                        cur_mid = (cb + ca) / 2.0
                        if future_mid < cur_mid - 1:
                            adverse_count += 1
                elif position == -1:
                    # Close short
                    pnl = entry_price - fill_px - cfg.half_cost_pts
                    trade_pnls.append(pnl)
                    if current_day:
                        daily_pnl[current_day] += pnl
                        daily_trades[current_day] += 1
                    n_fills_buy += 1
                    position = 0
                q_active = False
                continue

            # Sell fill: ask rose through our ask level
            if position >= 0 and ca > q_ask and prev_a <= q_ask:
                fill_px = q_ask
                if position == 0:
                    position = -1
                    entry_price = fill_px - cfg.half_cost_pts
                    entry_tick = i
                    trade_spreads.append(spread)
                    n_fills_sell += 1
                    if i + 5 < n:
                        future_mid = (bid[i + 5] + ask[i + 5]) / 2.0
                        cur_mid = (cb + ca) / 2.0
                        if future_mid > cur_mid + 1:
                            adverse_count += 1
                elif position == 1:
                    # Close long
                    pnl = fill_px - entry_price - cfg.half_cost_pts
                    trade_pnls.append(pnl)
                    if current_day:
                        daily_pnl[current_day] += pnl
                        daily_trades[current_day] += 1
                    n_fills_sell += 1
                    position = 0
                q_active = False
                continue

        # Generate quotes when spread is wide
        if spread >= cfg.spread_threshold_pts and position == 0:
            # Simple: quote at market bid + 1, ask - 1 (price improvement)
            q_bid = cb + 1  # improve bid by 1 pt
            q_ask = ca - 1  # improve ask by 1 pt
            q_live_ts = ct + cfg.latency_ns
            q_active = True

        # If we have a position, quote on the other side to close
        elif position == 1 and spread >= cfg.spread_threshold_pts:
            # Long: place ask to close
            q_ask = ca - 1
            q_bid = 0  # no bid
            q_live_ts = ct + cfg.latency_ns
            q_active = True
        elif position == -1 and spread >= cfg.spread_threshold_pts:
            # Short: place bid to close
            q_bid = cb + 1
            q_ask = 999999  # no ask
            q_live_ts = ct + cfg.latency_ns
            q_active = True

    # Force close at end
    if position != 0:
        mid = (bid[-1] + ask[-1]) / 2.0
        pnl = (mid - entry_price) * position - cfg.half_cost_pts
        trade_pnls.append(pnl)
        if current_day:
            daily_pnl[current_day] += pnl

    # Stats
    pnls = np.array(trade_pnls) if trade_pnls else np.array([0.0])
    n_days = len(daily_pnl)
    dpnl = np.array(list(daily_pnl.values())) if daily_pnl else np.array([0.0])
    n_total_fills = n_fills_buy + n_fills_sell

    # Max drawdown
    cum = np.cumsum(dpnl)
    peak = np.maximum.accumulate(cum)
    max_dd = float(np.min(cum - peak)) if len(cum) > 0 else 0.0

    results = {
        "config": {
            "spread_threshold_pts": cfg.spread_threshold_pts,
            "rt_cost_pts": cfg.rt_cost_pts,
            "latency_ns": cfg.latency_ns,
            "max_hold_ticks": cfg.max_hold_ticks,
        },
        "summary": {
            "total_pnl_pts": round(float(np.sum(pnls)), 2),
            "total_pnl_ntd": round(float(np.sum(pnls)) * 10, 2),
            "n_fills": n_total_fills,
            "n_buys": n_fills_buy,
            "n_sells": n_fills_sell,
            "n_round_trips": len(trade_pnls),
            "n_days": n_days,
            "fills_per_day": round(n_total_fills / max(1, n_days), 1),
            "rts_per_day": round(len(trade_pnls) / max(1, n_days), 1),
        },
        "pnl_stats": {
            "mean_pnl_per_rt_pts": round(float(np.mean(pnls)), 3),
            "median_pnl_per_rt_pts": round(float(np.median(pnls)), 3),
            "std_pnl_per_rt_pts": round(float(np.std(pnls)), 3),
            "win_rate": round(float(np.mean(pnls > 0)), 3) if len(pnls) > 1 else 0.0,
        },
        "adverse_selection": {
            "total_adverse": adverse_count,
            "adverse_rate": round(adverse_count / max(1, n_total_fills), 3),
        },
        "daily_pnl": {k: round(v, 2) for k, v in sorted(daily_pnl.items())},
        "daily_trades": {k: v for k, v in sorted(daily_trades.items())},
        "daily_stats": {
            "mean_daily_pnl_pts": round(float(np.mean(dpnl)), 2),
            "std_daily_pnl_pts": round(float(np.std(dpnl)), 2),
            "sharpe_daily": round(
                float(np.mean(dpnl) / np.std(dpnl)) * np.sqrt(252)
                if np.std(dpnl) > 0 else 0.0, 2,
            ),
            "win_days": int(np.sum(dpnl > 0)),
            "lose_days": int(np.sum(dpnl < 0)),
            "max_dd_pts": round(max_dd, 2),
        },
    }
    return results


def main() -> None:
    cfg = Config()
    if len(sys.argv) > 1:
        cfg.spread_threshold_pts = int(sys.argv[1])

    print(f"TMFD6 OpMM backtest: threshold={cfg.spread_threshold_pts}pts, cost={cfg.rt_cost_pts}pts, latency={cfg.latency_ns // 1_000_000}ms")
    results = run_backtest(cfg)

    s = results["summary"]
    p = results["pnl_stats"]
    a = results["adverse_selection"]
    d = results["daily_stats"]

    print(f"\n{'='*60}")
    print(f"Period: {s['n_days']} days | Fills: {s['n_fills']} ({s['fills_per_day']}/day)")
    print(f"Round trips: {s['n_round_trips']} ({s['rts_per_day']}/day)")
    print(f"\nPnL: {s['total_pnl_pts']:+.1f} pts ({s['total_pnl_ntd']:+.0f} NTD)")
    print(f"Per RT: mean={p['mean_pnl_per_rt_pts']:+.3f}, median={p['median_pnl_per_rt_pts']:+.3f}")
    print(f"Win rate: {p['win_rate']:.1%}")
    print(f"Adverse selection (5-tick): {a['adverse_rate']:.1%}")
    print(f"\nDaily: mean={d['mean_daily_pnl_pts']:+.1f}, Sharpe={d['sharpe_daily']:.2f}")
    print(f"Win days: {d['win_days']}/{s['n_days']}, Max DD: {d['max_dd_pts']:.1f} pts")

    print(f"\nDaily:")
    for day in sorted(results["daily_pnl"]):
        t = results["daily_trades"].get(day, 0)
        pnl = results["daily_pnl"][day]
        print(f"  {day}: {pnl:+8.1f} pts ({t:3d} RTs)")

    out_path = Path(cfg.output_dir) / f"backtest_opmm_thr{cfg.spread_threshold_pts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
