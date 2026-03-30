"""
OpportunisticMM Backtest V3 on TMFD6 (Micro-TAIEX Futures)

Correct fill model:
  - We post at best bid and best ask when spread >= threshold
  - BID FILL: Someone sells aggressively -> the bid at our level is consumed.
    Detected when: next tick's bid price drops BELOW our posted bid
    (our order got hit before the bid moved down)
  - ASK FILL: Someone buys aggressively -> the ask at our level is consumed.
    Detected when: next tick's ask price rises ABOVE our posted ask

  L1 (optimistic): Fill when bid drops to or below our level (bid taken)
  L2 (conservative): Fill when bid drops strictly below our level (price traded through)

After one-sided fill, we have inventory. Close immediately at market (next tick's best price).

RT cost = 4 points (40 NTD)
Point value = 10 NTD/point
"""
from __future__ import annotations

import numpy as np
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


def simulate_day(
    day: str,
    threshold_pts: int,
    fill_model: str,  # "L1" or "L2"
    close_mode: str = "immediate",  # "immediate" = close next tick, "passive" = wait for other side fill
) -> dict[str, Any] | None:
    """
    OpMM simulation with corrected fill model.

    Fill detection from BidAsk stream:
    - Posted bid at price B. If next update shows bid < B, we got filled at B
      (aggressive seller took our bid level).
    - Posted ask at price A. If next update shows ask > A, we got filled at A
      (aggressive buyer took our ask level).
    """
    data = load_day(day)
    if data is None:
        return None

    ts = data["exch_ts"]
    bid_raw = data["bid1_price"]  # scaled x1M
    ask_raw = data["ask1_price"]  # scaled x1M
    n = len(ts)
    if n < 100:
        return None

    # Convert to points
    bid_pts = bid_raw / PRICE_SCALE
    ask_pts = ask_raw / PRICE_SCALE
    spread_pts = ((ask_raw - bid_raw) // PRICE_SCALE).astype(np.int64)
    mid_pts = (bid_pts + ask_pts) / 2.0

    # Track trades
    trades_gross: list[float] = []  # gross PnL per RT in points
    trade_types: list[str] = []  # "both", "bid_then_close", "ask_then_close"
    holding_times_s: list[float] = []

    n_opps = 0
    n_cancel = 0

    # State
    state = "FLAT"
    posted_bid = 0.0
    posted_ask = 0.0
    post_idx = 0
    inv_side = ""  # "long" or "short"
    inv_entry_price = 0.0
    inv_entry_idx = 0

    for i in range(1, n):
        cur_bid = bid_pts[i]
        cur_ask = ask_pts[i]
        cur_spread = int(spread_pts[i])
        prev_bid = bid_pts[i - 1]
        prev_ask = ask_pts[i - 1]

        if state == "FLAT":
            if cur_spread >= threshold_pts:
                n_opps += 1
                posted_bid = cur_bid
                posted_ask = cur_ask
                post_idx = i
                state = "QUOTING"

        elif state == "QUOTING":
            # Check for fills by looking at price changes
            # Our bid fills if the bid dropped (someone sold into us)
            bid_fill = False
            ask_fill = False

            if fill_model == "L1":
                # Bid taken: current bid < our posted bid (our level consumed)
                bid_fill = cur_bid < posted_bid
                # Ask taken: current ask > our posted ask (our level consumed)
                ask_fill = cur_ask > posted_ask
            else:  # L2
                # Stricter: price must move at least 1 point through
                bid_fill = cur_bid < posted_bid - 0.5
                ask_fill = cur_ask > posted_ask + 0.5

            if bid_fill and ask_fill:
                # Both legs filled - perfect round trip!
                gross = posted_ask - posted_bid
                trades_gross.append(gross)
                trade_types.append("both")
                holding_times_s.append((ts[i] - ts[post_idx]) / 1e9)
                state = "FLAT"
            elif bid_fill:
                # We bought at posted_bid
                inv_side = "long"
                inv_entry_price = posted_bid
                inv_entry_idx = i
                if close_mode == "immediate":
                    state = "CLOSING_LONG"
                else:
                    state = "PASSIVE_CLOSE_LONG"
            elif ask_fill:
                # We sold at posted_ask
                inv_side = "short"
                inv_entry_price = posted_ask
                inv_entry_idx = i
                if close_mode == "immediate":
                    state = "CLOSING_SHORT"
                else:
                    state = "PASSIVE_CLOSE_SHORT"
            else:
                # Check if our quotes are still valid (spread still wide enough)
                # If spread narrowed, cancel
                if cur_spread < threshold_pts:
                    state = "FLAT"
                    n_cancel += 1
                # If someone improved bid above our bid, our order is stale
                elif cur_bid > posted_bid + 0.5:
                    state = "FLAT"
                    n_cancel += 1
                # If someone improved ask below our ask, our order is stale
                elif cur_ask < posted_ask - 0.5:
                    state = "FLAT"
                    n_cancel += 1

        elif state == "CLOSING_LONG":
            # We're long, sell at current bid (market sell)
            exit_price = cur_bid
            gross = exit_price - inv_entry_price
            trades_gross.append(gross)
            trade_types.append("bid_then_close")
            holding_times_s.append((ts[i] - ts[inv_entry_idx]) / 1e9)
            state = "FLAT"

        elif state == "CLOSING_SHORT":
            # We're short, buy at current ask (market buy)
            exit_price = cur_ask
            gross = inv_entry_price - exit_price
            trades_gross.append(gross)
            trade_types.append("ask_then_close")
            holding_times_s.append((ts[i] - ts[inv_entry_idx]) / 1e9)
            state = "FLAT"

        elif state == "PASSIVE_CLOSE_LONG":
            # Wait for ask to be taken (sell passively)
            # Post ask at entry_price + threshold (or just posted_ask)
            if cur_ask > posted_ask:
                # Our ask was taken!
                gross = posted_ask - inv_entry_price
                trades_gross.append(gross)
                trade_types.append("bid_passive_close")
                holding_times_s.append((ts[i] - ts[inv_entry_idx]) / 1e9)
                state = "FLAT"
            elif (ts[i] - ts[inv_entry_idx]) > 10_000_000_000:
                # Timeout: close at market
                exit_price = cur_bid
                gross = exit_price - inv_entry_price
                trades_gross.append(gross)
                trade_types.append("bid_timeout_close")
                holding_times_s.append((ts[i] - ts[inv_entry_idx]) / 1e9)
                state = "FLAT"

        elif state == "PASSIVE_CLOSE_SHORT":
            if cur_bid < posted_bid:
                gross = inv_entry_price - posted_bid
                trades_gross.append(gross)
                trade_types.append("ask_passive_close")
                holding_times_s.append((ts[i] - ts[inv_entry_idx]) / 1e9)
                state = "FLAT"
            elif (ts[i] - ts[inv_entry_idx]) > 10_000_000_000:
                exit_price = cur_ask
                gross = inv_entry_price - exit_price
                trades_gross.append(gross)
                trade_types.append("ask_timeout_close")
                holding_times_s.append((ts[i] - ts[inv_entry_idx]) / 1e9)
                state = "FLAT"

    # Close any remaining position
    if state in ("CLOSING_LONG", "PASSIVE_CLOSE_LONG"):
        gross = bid_pts[-1] - inv_entry_price
        trades_gross.append(gross)
        trade_types.append("eod_close")
        holding_times_s.append(0)
    elif state in ("CLOSING_SHORT", "PASSIVE_CLOSE_SHORT"):
        gross = inv_entry_price - ask_pts[-1]
        trades_gross.append(gross)
        trade_types.append("eod_close")
        holding_times_s.append(0)

    # Stats
    n_rt = len(trades_gross)
    gross_total = sum(trades_gross)
    net_total = gross_total - n_rt * RT_COST_PTS
    net_ntd = net_total * POINT_VALUE_NTD

    # By type
    both_count = sum(1 for t in trade_types if t == "both")
    both_gross = sum(g for g, t in zip(trades_gross, trade_types) if t == "both")
    close_count = n_rt - both_count
    close_gross = sum(g for g, t in zip(trades_gross, trade_types) if t != "both")
    close_adverse = sum(1 for g, t in zip(trades_gross, trade_types) if t != "both" and g < 0)

    # Drawdown
    pnl_series = [g - RT_COST_PTS for g in trades_gross]
    if pnl_series:
        cumsum = np.cumsum(pnl_series)
        peak = np.maximum.accumulate(cumsum)
        max_dd = float(np.max(peak - cumsum))
    else:
        max_dd = 0.0

    return {
        "day": day,
        "n_ticks": n,
        "n_opps": n_opps,
        "n_cancel": n_cancel,
        "n_trades": n_rt,
        "n_both": both_count,
        "n_close": close_count,
        "both_gross": round(both_gross, 1),
        "close_gross": round(close_gross, 1),
        "close_adverse_rate": round(close_adverse / max(close_count, 1), 3),
        "gross_pts": round(gross_total, 1),
        "net_pts": round(net_total, 1),
        "net_ntd": round(net_ntd, 0),
        "max_dd_pts": round(max_dd, 1),
        "avg_hold_s": round(float(np.mean(holding_times_s)), 3) if holding_times_s else 0,
        "avg_gross_per_rt": round(gross_total / max(n_rt, 1), 2),
    }


def print_summary_table(
    all_results: dict[tuple[str, int, str, str], list[dict[str, Any]]],
    fill_models: list[str],
    thresholds: list[int],
    close_modes: list[str],
    is_days: list[str],
    oos_days: list[str],
    all_days: list[str],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []

    print(f"\n{'FM':<3} {'CM':<6} {'Thr':>3} {'Per':<4}| {'D':>3} {'RT':>6} {'Both':>5} {'1s':>5} "
          f"{'BothGr':>7} {'1sGr':>7} {'Gross':>8} {'Net':>8} {'Net/d':>7} {'NTD/d':>7} "
          f"{'Sharpe':>6} {'1sAdv':>5} {'Gr/RT':>6} {'DD':>6}")
    print("-" * 120)

    for fm in fill_models:
        for cm in close_modes:
            for thr in thresholds:
                for pname, pdays in [("IS", is_days), ("OOS", oos_days), ("ALL", all_days)]:
                    key = (fm, thr, cm, pname)
                    results = all_results.get(key, [])
                    if not results:
                        continue
                    nd = len(results)
                    tot_rt = sum(r["n_trades"] for r in results)
                    tot_both = sum(r["n_both"] for r in results)
                    tot_1s = sum(r["n_close"] for r in results)
                    tot_both_gr = sum(r["both_gross"] for r in results)
                    tot_1s_gr = sum(r["close_gross"] for r in results)
                    tot_gross = sum(r["gross_pts"] for r in results)
                    tot_net = sum(r["net_pts"] for r in results)
                    daily_nets = [r["net_pts"] for r in results]
                    avg_net = float(np.mean(daily_nets))
                    std_net = float(np.std(daily_nets)) if nd > 1 else 1.0
                    sharpe = avg_net / std_net * np.sqrt(252) if std_net > 0 else 0.0
                    avg_adv = float(np.mean([r["close_adverse_rate"] for r in results]))
                    max_dd = max(r["max_dd_pts"] for r in results)
                    avg_ntd = float(np.mean([r["net_ntd"] for r in results]))
                    gr_per_rt = tot_gross / max(tot_rt, 1)

                    row = {
                        "fm": fm, "cm": cm, "thr": thr, "period": pname,
                        "n_days": nd, "tot_rt": tot_rt, "tot_both": tot_both,
                        "tot_1s": tot_1s, "tot_both_gr": tot_both_gr,
                        "tot_1s_gr": tot_1s_gr, "gross": tot_gross, "net": tot_net,
                        "net_per_day": round(avg_net, 1), "ntd_per_day": round(avg_ntd, 0),
                        "sharpe": round(sharpe, 2), "adv_pct": round(avg_adv * 100, 1),
                        "gr_per_rt": round(gr_per_rt, 2), "max_dd": round(max_dd, 1),
                    }
                    summaries.append(row)

                    print(f"{fm:<3} {cm:<6} {thr:>3} {pname:<4}| {nd:>3} {tot_rt:>6} {tot_both:>5} "
                          f"{tot_1s:>5} {tot_both_gr:>7.0f} {tot_1s_gr:>7.0f} "
                          f"{tot_gross:>8.0f} {tot_net:>8.0f} {avg_net:>7.0f} {avg_ntd:>7.0f} "
                          f"{sharpe:>6.1f} {avg_adv*100:>4.0f}% {gr_per_rt:>6.1f} {max_dd:>6.0f}")

    return summaries


def run_all() -> dict[str, Any]:
    all_files = sorted(DATA_DIR.glob("tmfd6_*.npz"))
    all_days = [f.stem.replace("tmfd6_", "") for f in all_files]
    print(f"Found {len(all_days)} days of data")

    is_days = [d for d in all_days if d < "2026-02-23"]
    oos_days = [d for d in all_days if d >= "2026-02-23"]
    print(f"IS: {len(is_days)} days ({is_days[0]}..{is_days[-1]})")
    print(f"OOS: {len(oos_days)} days ({oos_days[0]}..{oos_days[-1]})")

    fill_models = ["L1", "L2"]
    thresholds = [3, 5, 6, 8, 10, 15, 20, 30]
    close_modes = ["immediate"]  # passive adds complexity; start with immediate

    all_results: dict[tuple[str, int, str, str], list[dict[str, Any]]] = {}

    for fm in fill_models:
        for cm in close_modes:
            for thr in thresholds:
                for pname, pdays in [("IS", is_days), ("OOS", oos_days), ("ALL", all_days)]:
                    results = []
                    for day in pdays:
                        r = simulate_day(day, thr, fm, cm)
                        if r is not None:
                            results.append(r)
                    all_results[(fm, thr, cm, pname)] = results

    summaries = print_summary_table(all_results, fill_models, thresholds, close_modes,
                                     is_days, oos_days, all_days)

    # Per-day detail for key configs
    for fm, thr in [("L1", 5), ("L1", 10), ("L1", 20), ("L2", 10)]:
        print(f"\n=== PER-DAY: {fm}, threshold={thr}, immediate close ===")
        for day in all_days:
            r = simulate_day(day, thr, fm, "immediate")
            if r and r["n_trades"] > 0:
                marker = "IS " if day in is_days else "OOS"
                print(f"  {day} [{marker}]: RT={r['n_trades']:>5} (both={r['n_both']:>4}, 1s={r['n_close']:>4}), "
                      f"gross={r['gross_pts']:>7.0f}, net={r['net_pts']:>7.0f} ({r['net_ntd']:>7.0f} NTD), "
                      f"adv={r['close_adverse_rate']:.0%}, gr/rt={r['avg_gross_per_rt']:>+.1f}, "
                      f"hold={r['avg_hold_s']:.2f}s")

    # Also try passive close
    print("\n\n=== PASSIVE CLOSE MODE ===")
    passive_results: dict[tuple[str, int, str, str], list[dict[str, Any]]] = {}
    for fm in ["L1"]:
        for thr in [5, 10, 20]:
            for pname, pdays in [("IS", is_days), ("OOS", oos_days), ("ALL", all_days)]:
                results = []
                for day in pdays:
                    r = simulate_day(day, thr, fm, "passive")
                    if r is not None:
                        results.append(r)
                passive_results[(fm, thr, "passive", pname)] = results

    print_summary_table(passive_results, ["L1"], [5, 10, 20], ["passive"],
                        is_days, oos_days, all_days)

    # Save
    output_path = Path(__file__).parent / "results_v3.json"
    with open(output_path, "w") as f:
        json.dump({"summaries": summaries}, f, indent=2)
    print(f"\nSaved to {output_path}")
    return {"summaries": summaries}


if __name__ == "__main__":
    run_all()
