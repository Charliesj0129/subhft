"""
OpportunisticMM Backtest V2 on TMFD6 (Micro-TAIEX Futures)

Key differences from V1:
- Proper two-leg fill model: Post bid AND ask simultaneously, earn spread when BOTH fill
- Inventory risk: Track position from one-sided fills, close at market
- Realistic queue model: Don't assume immediate fill, track queue position
- No artificial timeout forcing bad exits

Strategy: Quote both sides when spread >= threshold.
  - Post at best bid + best ask
  - Earn spread when both legs fill
  - One-sided fill = inventory risk

RT cost = 4 points (40 NTD)
Point value = 10 NTD/point
"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json


PRICE_SCALE = 1_000_000
RT_COST_PTS = 4
POINT_VALUE_NTD = 10
DATA_DIR = Path(__file__).parent / "data"


def load_day(day: str) -> dict[str, np.ndarray] | None:
    fpath = DATA_DIR / f"tmfd6_{day}.npz"
    if not fpath.exists():
        return None
    data = np.load(fpath)
    return {k: data[k] for k in data.files}


@dataclass
class TradeRecord:
    entry_side: str  # "bid" or "ask" (which side filled first)
    entry_price_pts: float
    exit_price_pts: float
    gross_pnl_pts: float
    holding_ticks: int
    holding_time_s: float
    was_timeout: bool


def simulate_day_v2(
    day: str,
    threshold_pts: int,
    fill_model: str,  # "L1" or "L2"
    latency_ticks: int = 1,  # minimum ticks before fill possible
    max_hold_ticks: int = 0,  # 0 = no limit (close at EOD only)
    inventory_limit: int = 1,  # max abs position
) -> dict[str, Any] | None:
    """
    Improved OpMM simulation.

    Strategy logic:
    1. When flat and spread >= threshold: post bid at best_bid, ask at best_ask
    2. Track if bid or ask fills
    3. If one side fills -> we have inventory, immediately try to close other side
    4. If BOTH fill in same tick -> perfect RT, earn full spread
    5. When inventory, close at next available price (market)
    """
    data = load_day(day)
    if data is None:
        return None

    ts = data["exch_ts"]
    bid_px = data["bid1_price"]
    ask_px = data["ask1_price"]
    n = len(ts)
    if n < 100:
        return None

    spread_pts = ((ask_px - bid_px) // PRICE_SCALE).astype(np.int64)
    mid_pts = (bid_px.astype(np.float64) + ask_px.astype(np.float64)) / (2.0 * PRICE_SCALE)

    trades: list[TradeRecord] = []
    n_opps = 0

    # State machine
    state = "FLAT"  # FLAT, QUOTING, LONG, SHORT
    posted_bid_pts = 0.0
    posted_ask_pts = 0.0
    post_tick = 0
    inventory_entry_pts = 0.0
    inventory_entry_tick = 0

    for i in range(n):
        cur_bid = bid_px[i] / PRICE_SCALE
        cur_ask = ask_px[i] / PRICE_SCALE
        cur_spread = int(spread_pts[i])
        cur_mid = mid_pts[i]

        if state == "FLAT":
            if cur_spread >= threshold_pts:
                n_opps += 1
                posted_bid_pts = cur_bid
                posted_ask_pts = cur_ask
                post_tick = i
                state = "QUOTING"

        elif state == "QUOTING":
            if i < post_tick + latency_ticks:
                continue

            # Check if spread has narrowed and our quotes are no longer valid
            # (someone else improved the price)
            if cur_bid > posted_bid_pts or cur_ask < posted_ask_pts:
                # Market moved, our quotes are stale/crossed
                state = "FLAT"
                continue

            if cur_spread < threshold_pts:
                # Spread narrowed, cancel quotes
                state = "FLAT"
                continue

            # Check fills
            bid_filled = False
            ask_filled = False

            if fill_model == "L1":
                # Bid fills if someone lifts our bid (market ask touches our bid)
                # In practice: if current ask <= our posted bid (cross)
                # OR: if the best bid moves away (someone sold into us)
                # Simplified: bid fills if current bid < posted_bid (someone sold through)
                bid_filled = cur_ask <= posted_bid_pts
                ask_filled = cur_bid >= posted_ask_pts
            else:  # L2
                bid_filled = cur_ask < posted_bid_pts
                ask_filled = cur_bid > posted_ask_pts

            if bid_filled and ask_filled:
                # Both legs filled -> perfect round trip
                gross = posted_ask_pts - posted_bid_pts
                trades.append(TradeRecord(
                    entry_side="both",
                    entry_price_pts=posted_bid_pts,
                    exit_price_pts=posted_ask_pts,
                    gross_pnl_pts=gross,
                    holding_ticks=i - post_tick,
                    holding_time_s=(ts[i] - ts[post_tick]) / 1e9,
                    was_timeout=False,
                ))
                state = "FLAT"
            elif bid_filled:
                # We bought at posted_bid_pts, now need to sell
                inventory_entry_pts = posted_bid_pts
                inventory_entry_tick = i
                state = "LONG"
            elif ask_filled:
                # We sold at posted_ask_pts, now need to buy
                inventory_entry_pts = posted_ask_pts
                inventory_entry_tick = i
                state = "SHORT"

        elif state == "LONG":
            # Close long position: sell at current bid
            # Wait at least 1 tick after entry
            if i > inventory_entry_tick:
                exit_price = cur_bid
                gross = exit_price - inventory_entry_pts
                trades.append(TradeRecord(
                    entry_side="bid",
                    entry_price_pts=inventory_entry_pts,
                    exit_price_pts=exit_price,
                    gross_pnl_pts=gross,
                    holding_ticks=i - inventory_entry_tick,
                    holding_time_s=(ts[i] - ts[inventory_entry_tick]) / 1e9,
                    was_timeout=False,
                ))
                state = "FLAT"

        elif state == "SHORT":
            # Close short position: buy at current ask
            if i > inventory_entry_tick:
                exit_price = cur_ask
                gross = inventory_entry_pts - exit_price
                trades.append(TradeRecord(
                    entry_side="ask",
                    entry_price_pts=inventory_entry_pts,
                    exit_price_pts=exit_price,
                    gross_pnl_pts=gross,
                    holding_ticks=i - inventory_entry_tick,
                    holding_time_s=(ts[i] - ts[inventory_entry_tick]) / 1e9,
                    was_timeout=False,
                ))
                state = "FLAT"

    # Close any remaining position at last mid
    if state == "LONG":
        gross = mid_pts[-1] - inventory_entry_pts
        trades.append(TradeRecord("bid", inventory_entry_pts, mid_pts[-1], gross, n - inventory_entry_tick, 0, True))
    elif state == "SHORT":
        gross = inventory_entry_pts - mid_pts[-1]
        trades.append(TradeRecord("ask", inventory_entry_pts, mid_pts[-1], gross, n - inventory_entry_tick, 0, True))

    # Compute stats
    n_rt = len(trades)
    gross_pnl = sum(t.gross_pnl_pts for t in trades)
    net_pnl = gross_pnl - n_rt * RT_COST_PTS

    # Separate by entry type
    both_legs = [t for t in trades if t.entry_side == "both"]
    one_sided = [t for t in trades if t.entry_side in ("bid", "ask")]

    # PnL breakdown
    both_gross = sum(t.gross_pnl_pts for t in both_legs)
    one_gross = sum(t.gross_pnl_pts for t in one_sided)

    # Adverse selection: for one-sided fills, how often is gross_pnl < 0?
    one_adverse = sum(1 for t in one_sided if t.gross_pnl_pts < 0)
    one_adv_rate = one_adverse / max(len(one_sided), 1)

    # Drawdown
    pnl_series = [t.gross_pnl_pts - RT_COST_PTS for t in trades]
    cumsum = np.cumsum(pnl_series) if pnl_series else np.array([0.0])
    peak = np.maximum.accumulate(cumsum)
    max_dd = float(np.max(peak - cumsum)) if len(cumsum) > 0 else 0.0

    return {
        "day": day,
        "n_ticks": n,
        "n_opps": n_opps,
        "n_trades": n_rt,
        "n_both_fill": len(both_legs),
        "n_one_sided": len(one_sided),
        "both_gross_pts": round(both_gross, 1),
        "one_gross_pts": round(one_gross, 1),
        "gross_pnl_pts": round(gross_pnl, 1),
        "net_pnl_pts": round(net_pnl, 1),
        "net_pnl_ntd": round(net_pnl * POINT_VALUE_NTD, 0),
        "one_sided_adv_rate": round(one_adv_rate, 3),
        "max_dd_pts": round(max_dd, 1),
        "avg_hold_s": round(np.mean([t.holding_time_s for t in trades]), 3) if trades else 0,
    }


def run_backtest_v2() -> dict[str, Any]:
    all_files = sorted(DATA_DIR.glob("tmfd6_*.npz"))
    all_days = [f.stem.replace("tmfd6_", "") for f in all_files]
    print(f"Found {len(all_days)} days")

    # IS/OOS split
    is_days = [d for d in all_days if d < "2026-02-23"]
    oos_days = [d for d in all_days if d >= "2026-02-23"]

    thresholds = [3, 5, 6, 8, 10, 15, 20, 30]
    fill_models = ["L1", "L2"]

    print(f"\n{'Model':<5} {'Thr':>4} {'Period':<4} | {'Days':>4} {'Trades':>7} {'Both':>6} {'1-Side':>6} "
          f"{'BothGr':>8} {'1sGr':>8} {'Gross':>8} {'Net':>8} {'Net/d':>7} {'NTD/d':>8} "
          f"{'Sharpe':>7} {'1sAdv%':>6} {'MaxDD':>7}")
    print("-" * 130)

    all_summaries: list[dict[str, Any]] = []

    for fm in fill_models:
        for thr in thresholds:
            for period_name, period_days in [("IS", is_days), ("OOS", oos_days), ("ALL", all_days)]:
                results = []
                for day in period_days:
                    r = simulate_day_v2(day, thr, fm)
                    if r is not None:
                        results.append(r)

                if not results:
                    continue

                nd = len(results)
                total_trades = sum(r["n_trades"] for r in results)
                total_both = sum(r["n_both_fill"] for r in results)
                total_1s = sum(r["n_one_sided"] for r in results)
                total_both_gr = sum(r["both_gross_pts"] for r in results)
                total_1s_gr = sum(r["one_gross_pts"] for r in results)
                total_gross = sum(r["gross_pnl_pts"] for r in results)
                total_net = sum(r["net_pnl_pts"] for r in results)
                daily_nets = [r["net_pnl_pts"] for r in results]
                avg_net_d = np.mean(daily_nets)
                std_net_d = np.std(daily_nets) if nd > 1 else 1.0
                sharpe = float(avg_net_d / std_net_d * np.sqrt(252)) if std_net_d > 0 else 0.0
                avg_adv = np.mean([r["one_sided_adv_rate"] for r in results])
                max_dd = max(r["max_dd_pts"] for r in results)
                avg_ntd = np.mean([r["net_pnl_ntd"] for r in results])

                summary = {
                    "model": fm, "threshold": thr, "period": period_name,
                    "n_days": nd, "total_trades": total_trades,
                    "total_both": total_both, "total_1s": total_1s,
                    "both_gross": round(total_both_gr, 1),
                    "one_sided_gross": round(total_1s_gr, 1),
                    "gross_pts": round(total_gross, 1),
                    "net_pts": round(total_net, 1),
                    "net_per_day": round(float(avg_net_d), 1),
                    "ntd_per_day": round(float(avg_ntd), 0),
                    "sharpe": round(sharpe, 2),
                    "one_sided_adv_pct": round(float(avg_adv * 100), 1),
                    "max_dd": round(max_dd, 1),
                }
                all_summaries.append(summary)

                print(f"{fm:<5} {thr:>4} {period_name:<4} | {nd:>4} {total_trades:>7} {total_both:>6} "
                      f"{total_1s:>6} {total_both_gr:>8.1f} {total_1s_gr:>8.1f} "
                      f"{total_gross:>8.1f} {total_net:>8.1f} {avg_net_d:>7.1f} {avg_ntd:>8.0f} "
                      f"{sharpe:>7.2f} {avg_adv*100:>5.1f}% {max_dd:>7.1f}")

    # Per-day detail for interesting configs
    print("\n=== PER-DAY DETAIL: L1, threshold=5 ===")
    for day in all_days:
        r = simulate_day_v2(day, 5, "L1")
        if r:
            marker = "IS " if day in is_days else "OOS"
            print(f"  {r['day']} [{marker}]: trades={r['n_trades']:>5} (both={r['n_both_fill']:>4}, "
                  f"1s={r['n_one_sided']:>4}), gross={r['gross_pnl_pts']:>8.1f}, "
                  f"net={r['net_pnl_pts']:>8.1f} ({r['net_pnl_ntd']:>8.0f} NTD), "
                  f"1s_adv={r['one_sided_adv_rate']:.1%}, hold={r['avg_hold_s']:.3f}s")

    print("\n=== PER-DAY DETAIL: L1, threshold=20 ===")
    for day in all_days:
        r = simulate_day_v2(day, 20, "L1")
        if r:
            marker = "IS " if day in is_days else "OOS"
            print(f"  {r['day']} [{marker}]: trades={r['n_trades']:>5} (both={r['n_both_fill']:>4}, "
                  f"1s={r['n_one_sided']:>4}), gross={r['gross_pnl_pts']:>8.1f}, "
                  f"net={r['net_pnl_pts']:>8.1f} ({r['net_pnl_ntd']:>8.0f} NTD), "
                  f"1s_adv={r['one_sided_adv_rate']:.1%}, hold={r['avg_hold_s']:.3f}s")

    # ---- V3: Smarter model - only earn spread on perfect fills, skip one-sided ----
    print("\n\n=== V3: SPREAD-ONLY MODEL (skip one-sided inventory risk) ===")
    print("  Only count trades where BOTH legs fill simultaneously")
    print(f"\n{'Model':<5} {'Thr':>4} {'Period':<4} | {'Days':>4} {'Both':>6} {'Both/d':>6} "
          f"{'AvgSprd':>7} {'Gross':>8} {'Net':>8} {'Net/d':>7} {'NTD/d':>8} {'Sharpe':>7}")
    print("-" * 100)

    for fm in fill_models:
        for thr in thresholds:
            for period_name, period_days in [("IS", is_days), ("OOS", oos_days), ("ALL", all_days)]:
                results = []
                for day in period_days:
                    r = simulate_day_v2(day, thr, fm)
                    if r is not None:
                        results.append(r)
                if not results:
                    continue

                nd = len(results)
                total_both = sum(r["n_both_fill"] for r in results)
                total_both_gr = sum(r["both_gross_pts"] for r in results)
                # Only count both-fill trades, each costs RT_COST_PTS
                total_both_net = total_both_gr - total_both * RT_COST_PTS
                daily_both_nets = [(r["both_gross_pts"] - r["n_both_fill"] * RT_COST_PTS) for r in results]
                avg_both_net_d = np.mean(daily_both_nets)
                std_d = np.std(daily_both_nets) if nd > 1 else 1.0
                sharpe = float(avg_both_net_d / std_d * np.sqrt(252)) if std_d > 0 else 0.0
                avg_spread = total_both_gr / max(total_both, 1)
                avg_ntd = avg_both_net_d * POINT_VALUE_NTD

                if period_name == "ALL" or (fm == "L1" and thr in [5, 10, 20]):
                    print(f"{fm:<5} {thr:>4} {period_name:<4} | {nd:>4} {total_both:>6} "
                          f"{total_both/nd:>6.1f} {avg_spread:>7.1f} {total_both_gr:>8.1f} "
                          f"{total_both_net:>8.1f} {avg_both_net_d:>7.1f} {avg_ntd:>8.0f} {sharpe:>7.2f}")

    # Save
    output_path = Path(__file__).parent / "results_v2.json"
    with open(output_path, "w") as f:
        json.dump({"summaries": all_summaries}, f, indent=2)
    print(f"\nSaved to {output_path}")

    return {"summaries": all_summaries}


if __name__ == "__main__":
    run_backtest_v2()
