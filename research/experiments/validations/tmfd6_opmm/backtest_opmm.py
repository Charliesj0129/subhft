"""
OpportunisticMM Backtest on TMFD6 (Micro-TAIEX Futures)

Strategy: Quote both sides ONLY when spread >= threshold.
Fill models:
  L1 (optimistic): Fill when opposite side crosses our level
  L2 (conservative): Fill only when price moves THROUGH our level

RT cost = 4 points (40 NTD = tax 7 + comm 13 per side, point_value=10 NTD)
Point value = 10 NTD/point
"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json


PRICE_SCALE = 1_000_000  # ClickHouse stores prices x1,000,000
RT_COST_PTS = 4  # Round-trip cost in points
POINT_VALUE_NTD = 10  # NTD per point

DATA_DIR = Path(__file__).parent / "data"


@dataclass
class DailyResult:
    day: str
    n_ticks: int
    n_opportunities: int  # ticks where spread >= threshold
    n_fills_bid: int
    n_fills_ask: int
    n_round_trips: int
    gross_pnl_pts: float  # before costs
    net_pnl_pts: float  # after costs
    net_pnl_ntd: float
    adverse_selection_rate: float  # fraction of fills with mid move against us within 1s
    avg_holding_time_s: float
    max_drawdown_pts: float


@dataclass
class Position:
    qty: int = 0  # +1 = long, -1 = short
    entry_price_pts: float = 0.0
    entry_ts: int = 0


def load_day(day: str) -> dict[str, np.ndarray] | None:
    fpath = DATA_DIR / f"tmfd6_{day}.npz"
    if not fpath.exists():
        return None
    data = np.load(fpath)
    return {k: data[k] for k in data.files}


def compute_mid_pts(bid_price: np.ndarray, ask_price: np.ndarray) -> np.ndarray:
    """Mid price in points (float). Prices arrive as scaled int x1M."""
    return (bid_price.astype(np.float64) + ask_price.astype(np.float64)) / (2.0 * PRICE_SCALE)


def compute_spread_pts(bid_price: np.ndarray, ask_price: np.ndarray) -> np.ndarray:
    """Spread in points (int). Prices arrive as scaled int x1M."""
    return ((ask_price - bid_price) // PRICE_SCALE).astype(np.int64)


def run_markout_analysis(days: list[str]) -> dict[str, Any]:
    """
    Adverse selection analysis: When spread widens from <=4 to >=5,
    what happens to mid-price in next 1s, 5s, 10s?
    """
    horizons_ns = [1_000_000_000, 5_000_000_000, 10_000_000_000]
    horizon_labels = ["1s", "5s", "10s"]
    markouts: dict[str, list[float]] = {h: [] for h in horizon_labels}

    for day in days:
        data = load_day(day)
        if data is None:
            continue

        ts = data["exch_ts"]
        mid = compute_mid_pts(data["bid1_price"], data["ask1_price"])
        spread = compute_spread_pts(data["bid1_price"], data["ask1_price"])
        n = len(ts)

        # Find transitions: spread[i-1] <= 4 and spread[i] >= 5
        for i in range(1, n):
            if spread[i - 1] <= 4 and spread[i] >= 5:
                entry_mid = mid[i]
                for h_idx, h_ns in enumerate(horizons_ns):
                    target_ts = ts[i] + h_ns
                    # Binary search for closest tick
                    j = np.searchsorted(ts[i:], target_ts) + i
                    if j < n:
                        markouts[horizon_labels[h_idx]].append(mid[j] - entry_mid)

    results = {}
    for h in horizon_labels:
        arr = np.array(markouts[h]) if markouts[h] else np.array([0.0])
        results[h] = {
            "count": len(markouts[h]),
            "mean_pts": float(np.mean(arr)),
            "std_pts": float(np.std(arr)),
            "median_pts": float(np.median(arr)),
            "pct_adverse_bid": float(np.mean(arr < 0) * 100),  # mid drops = adverse for bid
            "pct_adverse_ask": float(np.mean(arr > 0) * 100),  # mid rises = adverse for ask
        }
    return results


def simulate_day_opmm(
    day: str,
    threshold_pts: int,
    fill_model: str,  # "L1" or "L2"
    latency_ns: int = 36_000_000,  # 36ms RTT
) -> DailyResult | None:
    """
    Simulate OpportunisticMM for one day.

    L1 fill model: We post at best bid/ask when spread >= threshold.
        Fill happens when the opposite side's price reaches our posted level.
        (i.e., ask drops to our bid, or bid rises to our ask)

    L2 fill model: Fill only when price moves THROUGH our level.
        (i.e., ask drops BELOW our bid, or bid rises ABOVE our ask)
    """
    data = load_day(day)
    if data is None:
        return None

    ts = data["exch_ts"]
    bid_px = data["bid1_price"]  # scaled x1M
    ask_px = data["ask1_price"]  # scaled x1M
    bid_vol = data["bid1_vol"]
    ask_vol = data["ask1_vol"]
    n = len(ts)

    if n < 100:
        return None

    spread = compute_spread_pts(bid_px, ask_px)
    mid = compute_mid_pts(bid_px, ask_px)

    # State
    pos = Position()
    round_trips: list[float] = []  # PnL per RT in points
    fill_count_bid = 0
    fill_count_ask = 0
    n_opps = 0
    holding_times: list[float] = []
    adverse_count = 0
    total_fill_count = 0

    # Track our posted quotes
    posted_bid_pts: float | None = None  # price in points we're bidding at
    posted_ask_pts: float | None = None  # price in points we're asking at
    post_time_ns: int = 0

    for i in range(n):
        current_spread = spread[i]
        current_bid_pts = bid_px[i] / PRICE_SCALE
        current_ask_pts = ask_px[i] / PRICE_SCALE
        current_mid = mid[i]
        current_ts = ts[i]

        # Check fills on our posted quotes
        if pos.qty == 0 and posted_bid_pts is not None and posted_ask_pts is not None:
            # Only check fills after latency period
            if current_ts >= post_time_ns + latency_ns:
                # Check bid fill (someone sells to us)
                bid_filled = False
                if fill_model == "L1":
                    # Fill if market ask drops to our bid level
                    bid_filled = current_ask_pts <= posted_bid_pts
                else:  # L2
                    # Fill if market ask drops BELOW our bid level
                    bid_filled = current_ask_pts < posted_bid_pts

                # Check ask fill (someone buys from us)
                ask_filled = False
                if fill_model == "L1":
                    ask_filled = current_bid_pts >= posted_ask_pts
                else:  # L2
                    ask_filled = current_bid_pts > posted_ask_pts

                if bid_filled and not ask_filled:
                    pos.qty = 1
                    pos.entry_price_pts = posted_bid_pts
                    pos.entry_ts = current_ts
                    fill_count_bid += 1
                    total_fill_count += 1
                    posted_bid_pts = None
                    posted_ask_pts = None
                elif ask_filled and not bid_filled:
                    pos.qty = -1
                    pos.entry_price_pts = posted_ask_pts
                    pos.entry_ts = current_ts
                    fill_count_ask += 1
                    total_fill_count += 1
                    posted_bid_pts = None
                    posted_ask_pts = None
                elif bid_filled and ask_filled:
                    # Both sides filled simultaneously - count as RT
                    pnl = posted_ask_pts - posted_bid_pts
                    round_trips.append(pnl)
                    fill_count_bid += 1
                    fill_count_ask += 1
                    total_fill_count += 2
                    posted_bid_pts = None
                    posted_ask_pts = None

        # If we have a position, look to close it
        if pos.qty != 0:
            if pos.qty == 1:
                # We're long, close by selling at best bid
                if fill_model == "L1":
                    can_close = current_bid_pts >= pos.entry_price_pts
                else:
                    can_close = current_bid_pts > pos.entry_price_pts

                # Also close if spread opens wide enough (take profit)
                # or if we've been holding too long (10s timeout)
                holding_ns = current_ts - pos.entry_ts
                if can_close or holding_ns > 10_000_000_000:
                    exit_price = current_bid_pts
                    pnl = exit_price - pos.entry_price_pts
                    round_trips.append(pnl)
                    holding_times.append(holding_ns / 1e9)
                    fill_count_ask += 1
                    total_fill_count += 1

                    # Adverse selection: did mid move against us?
                    if current_mid < (pos.entry_price_pts + 0.5):  # mid below our entry
                        adverse_count += 1

                    pos = Position()

            elif pos.qty == -1:
                # We're short, close by buying at best ask
                if fill_model == "L1":
                    can_close = current_ask_pts <= pos.entry_price_pts
                else:
                    can_close = current_ask_pts < pos.entry_price_pts

                holding_ns = current_ts - pos.entry_ts
                if can_close or holding_ns > 10_000_000_000:
                    exit_price = current_ask_pts
                    pnl = pos.entry_price_pts - exit_price
                    round_trips.append(pnl)
                    holding_times.append(holding_ns / 1e9)
                    fill_count_bid += 1
                    total_fill_count += 1

                    if current_mid > (pos.entry_price_pts - 0.5):
                        adverse_count += 1

                    pos = Position()

        # Post new quotes if flat and spread is wide enough
        if pos.qty == 0 and posted_bid_pts is None:
            if current_spread >= threshold_pts:
                n_opps += 1
                # Post at best bid and best ask
                posted_bid_pts = current_bid_pts
                posted_ask_pts = current_ask_pts
                post_time_ns = current_ts

    # Close any remaining position at last mid
    if pos.qty != 0:
        if pos.qty == 1:
            pnl = mid[-1] - pos.entry_price_pts
        else:
            pnl = pos.entry_price_pts - mid[-1]
        round_trips.append(pnl)
        holding_times.append((ts[-1] - pos.entry_ts) / 1e9)

    n_rt = len(round_trips)
    gross_pnl = sum(round_trips) if round_trips else 0.0
    net_pnl = gross_pnl - n_rt * RT_COST_PTS
    net_pnl_ntd = net_pnl * POINT_VALUE_NTD

    # Max drawdown
    cumsum = np.cumsum([r - RT_COST_PTS for r in round_trips]) if round_trips else np.array([0.0])
    peak = np.maximum.accumulate(cumsum)
    dd = peak - cumsum
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

    adv_rate = adverse_count / max(total_fill_count, 1)

    return DailyResult(
        day=day,
        n_ticks=n,
        n_opportunities=n_opps,
        n_fills_bid=fill_count_bid,
        n_fills_ask=fill_count_ask,
        n_round_trips=n_rt,
        gross_pnl_pts=gross_pnl,
        net_pnl_pts=net_pnl,
        net_pnl_ntd=net_pnl_ntd,
        adverse_selection_rate=adv_rate,
        avg_holding_time_s=float(np.mean(holding_times)) if holding_times else 0.0,
        max_drawdown_pts=max_dd,
    )


def run_full_backtest() -> dict[str, Any]:
    """Run full backtest across all days and thresholds."""
    # Discover all days
    all_files = sorted(DATA_DIR.glob("tmfd6_*.npz"))
    all_days = [f.stem.replace("tmfd6_", "") for f in all_files]
    print(f"Found {len(all_days)} days of data")

    # ---- Step 1: Data Summary ----
    print("\n=== DATA SUMMARY ===")
    daily_stats: list[dict[str, Any]] = []
    for day in all_days:
        data = load_day(day)
        if data is None:
            continue
        n = len(data["exch_ts"])
        spread = compute_spread_pts(data["bid1_price"], data["ask1_price"])
        mid = compute_mid_pts(data["bid1_price"], data["ask1_price"])

        dt_ms = np.diff(data["exch_ts"].astype(np.float64)) / 1e6
        dt_ms = dt_ms[dt_ms > 0]

        stats = {
            "day": day,
            "n_ticks": n,
            "med_spread": int(np.median(spread)),
            "avg_spread": round(float(np.mean(spread)), 1),
            "pct_ge5": round(float(np.mean(spread >= 5) * 100), 1),
            "pct_ge8": round(float(np.mean(spread >= 8) * 100), 1),
            "pct_ge10": round(float(np.mean(spread >= 10) * 100), 1),
            "avg_spread_ge5": round(float(np.mean(spread[spread >= 5])), 1) if np.any(spread >= 5) else 0,
            "med_interval_ms": round(float(np.median(dt_ms)), 1) if len(dt_ms) > 0 else 0,
            "avg_bid_vol": round(float(np.mean(data["bid1_vol"])), 1),
            "avg_ask_vol": round(float(np.mean(data["ask1_vol"])), 1),
            "price_range_pts": round(float(np.max(mid) - np.min(mid)), 1),
        }
        daily_stats.append(stats)
        print(f"  {day}: n={n:>7}, med_spread={stats['med_spread']:>3}, "
              f"≥5={stats['pct_ge5']:>5.1f}%, ≥10={stats['pct_ge10']:>5.1f}%, "
              f"interval={stats['med_interval_ms']:>6.1f}ms, "
              f"bid_vol={stats['avg_bid_vol']:>5.1f}, ask_vol={stats['avg_ask_vol']:>5.1f}")

    # ---- Step 2: Spread Distribution ----
    print("\n=== SPREAD DISTRIBUTION (text histogram) ===")
    # Use two representative days
    for sample_day in ["2026-01-30", "2026-03-23"]:
        data = load_day(sample_day)
        if data is None:
            continue
        spread = compute_spread_pts(data["bid1_price"], data["ask1_price"])
        print(f"\n  {sample_day}:")
        bins = list(range(1, 51))
        for b in bins:
            count = int(np.sum(spread == b))
            pct = count / len(spread) * 100
            bar = "#" * int(pct * 2)
            if pct >= 0.5:
                print(f"    {b:>3} pts: {pct:>5.1f}% {bar}")

    # ---- Step 3: Markout Analysis ----
    print("\n=== MARKOUT ANALYSIS (spread <=4 -> >=5 transition) ===")
    markouts = run_markout_analysis(all_days)
    for horizon, stats in markouts.items():
        print(f"  {horizon}: n={stats['count']}, mean={stats['mean_pts']:+.3f} pts, "
              f"std={stats['std_pts']:.3f}, adverse_bid={stats['pct_adverse_bid']:.1f}%, "
              f"adverse_ask={stats['pct_adverse_ask']:.1f}%")

    # ---- Step 4: Backtest across thresholds and fill models ----
    print("\n=== BACKTEST RESULTS ===")
    thresholds = [3, 5, 6, 8, 10, 15, 20]
    fill_models = ["L1", "L2"]

    # Split IS/OOS: use day ordering
    # Jan-Feb (12 days) = IS, late Feb + Mar (9 days) = OOS
    is_days = [d for d in all_days if d < "2026-02-23"]
    oos_days = [d for d in all_days if d >= "2026-02-23"]
    print(f"  IS days ({len(is_days)}): {is_days[0]}..{is_days[-1]}")
    print(f"  OOS days ({len(oos_days)}): {oos_days[0]}..{oos_days[-1]}")

    all_results: dict[str, dict[str, list[DailyResult]]] = {}

    for fm in fill_models:
        all_results[fm] = {}
        for threshold in thresholds:
            results = []
            for day in all_days:
                r = simulate_day_opmm(day, threshold, fm)
                if r is not None:
                    results.append(r)
            all_results[fm][str(threshold)] = results

    # Print summary table
    print("\n  --- Summary Table ---")
    print(f"  {'Model':<5} {'Thr':>4} {'Period':<4} | {'Days':>4} {'RT':>6} {'RT/d':>5} "
          f"{'Gross':>8} {'Net':>8} {'Net/d':>7} {'NTD/d':>8} {'Sharpe':>7} {'AdvSel':>6} {'MaxDD':>7}")
    print("  " + "-" * 100)

    summary_rows: list[dict[str, Any]] = []

    for fm in fill_models:
        for threshold in thresholds:
            results = all_results[fm][str(threshold)]

            for period_name, period_days in [("IS", is_days), ("OOS", oos_days), ("ALL", all_days)]:
                period_results = [r for r in results if r.day in period_days]
                if not period_results:
                    continue

                n_days = len(period_results)
                total_rt = sum(r.n_round_trips for r in period_results)
                total_gross = sum(r.gross_pnl_pts for r in period_results)
                total_net = sum(r.net_pnl_pts for r in period_results)
                daily_nets = [r.net_pnl_pts for r in period_results]
                daily_ntd = [r.net_pnl_ntd for r in period_results]
                avg_net_d = np.mean(daily_nets)
                std_net_d = np.std(daily_nets) if len(daily_nets) > 1 else 1.0
                sharpe = float(avg_net_d / std_net_d * np.sqrt(252)) if std_net_d > 0 else 0.0
                avg_adv = np.mean([r.adverse_selection_rate for r in period_results])
                max_dd = max(r.max_drawdown_pts for r in period_results)

                row = {
                    "model": fm, "threshold": threshold, "period": period_name,
                    "n_days": n_days, "total_rt": total_rt,
                    "rt_per_day": round(total_rt / n_days, 1),
                    "gross_pts": round(total_gross, 1), "net_pts": round(total_net, 1),
                    "net_per_day_pts": round(float(avg_net_d), 1),
                    "ntd_per_day": round(float(np.mean(daily_ntd)), 0),
                    "sharpe": round(sharpe, 2),
                    "adv_sel": round(float(avg_adv * 100), 1),
                    "max_dd_pts": round(max_dd, 1),
                }
                summary_rows.append(row)

                print(f"  {fm:<5} {threshold:>4} {period_name:<4} | {n_days:>4} {total_rt:>6} "
                      f"{row['rt_per_day']:>5.1f} {total_gross:>8.1f} {total_net:>8.1f} "
                      f"{avg_net_d:>7.1f} {np.mean(daily_ntd):>8.0f} "
                      f"{sharpe:>7.2f} {avg_adv*100:>5.1f}% {max_dd:>7.1f}")

    # ---- Step 5: Per-day detail for best config ----
    print("\n=== PER-DAY DETAIL (L1, threshold=5) ===")
    for r in all_results["L1"]["5"]:
        marker = "IS" if r.day in is_days else "OOS"
        print(f"  {r.day} [{marker}]: RT={r.n_round_trips:>4}, gross={r.gross_pnl_pts:>7.1f}, "
              f"net={r.net_pnl_pts:>7.1f} pts ({r.net_pnl_ntd:>8.0f} NTD), "
              f"adv={r.adverse_selection_rate:.1%}, hold={r.avg_holding_time_s:.1f}s")

    # Save results
    output = {
        "daily_stats": daily_stats,
        "markouts": markouts,
        "summary_rows": summary_rows,
        "is_days": is_days,
        "oos_days": oos_days,
    }
    output_path = Path(__file__).parent / "results.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {output_path}")

    return output


if __name__ == "__main__":
    run_full_backtest()
