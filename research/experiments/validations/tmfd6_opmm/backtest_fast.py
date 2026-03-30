"""
Fast OpMM Backtest on TMFD6 using numpy vectorization where possible.

Fill model: We post at best bid/ask when spread >= threshold.
  - BID FILL: detected when bid drops below our posted level (someone sold into us)
  - ASK FILL: detected when ask rises above our posted level (someone bought from us)

After one-sided fill, close at market on next tick.
RT cost = 4 points (40 NTD), Point value = 10 NTD/point
"""
from __future__ import annotations
import numpy as np
from pathlib import Path
import json
import sys

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


def simulate_day(day: str, threshold_pts: int, fill_model: str) -> dict[str, float] | None:
    """Tick-by-tick simulation with minimal Python overhead."""
    data = load_day(day)
    if data is None:
        return None

    ts = data["exch_ts"]
    bid_raw = data["bid1_price"]
    ask_raw = data["ask1_price"]
    n = len(ts)
    if n < 100:
        return None

    # Work in points (float64 for speed)
    bid = bid_raw.astype(np.float64) / PRICE_SCALE
    ask = ask_raw.astype(np.float64) / PRICE_SCALE
    spread = (ask - bid)
    mid = (bid + ask) / 2.0

    # State machine: 0=FLAT, 1=QUOTING, 2=LONG(closing), 3=SHORT(closing)
    state = 0
    posted_bid = 0.0
    posted_ask = 0.0
    inv_entry = 0.0

    gross_pnl_total = 0.0
    n_trades = 0
    n_both = 0
    n_onesided = 0
    n_opps = 0
    onesided_adverse = 0
    both_gross_total = 0.0
    onesided_gross_total = 0.0

    strict = (fill_model == "L2")

    for i in range(1, n):
        cb = bid[i]
        ca = ask[i]
        cs = spread[i]

        if state == 0:  # FLAT
            if cs >= threshold_pts:
                n_opps += 1
                posted_bid = cb
                posted_ask = ca
                state = 1

        elif state == 1:  # QUOTING
            # Check fills
            bf = (cb < posted_bid - 0.5) if strict else (cb < posted_bid)
            af = (ca > posted_ask + 0.5) if strict else (ca > posted_ask)

            if bf and af:
                g = posted_ask - posted_bid
                gross_pnl_total += g
                both_gross_total += g
                n_trades += 1
                n_both += 1
                state = 0
            elif bf:
                inv_entry = posted_bid
                state = 2  # LONG
            elif af:
                inv_entry = posted_ask
                state = 3  # SHORT
            else:
                # Cancel if spread narrowed or quote stale
                if cs < threshold_pts or cb > posted_bid + 0.5 or ca < posted_ask - 0.5:
                    state = 0

        elif state == 2:  # LONG - close at market (sell at bid)
            g = cb - inv_entry
            gross_pnl_total += g
            onesided_gross_total += g
            n_trades += 1
            n_onesided += 1
            if g < 0:
                onesided_adverse += 1
            state = 0

        elif state == 3:  # SHORT - close at market (buy at ask)
            g = inv_entry - ca
            gross_pnl_total += g
            onesided_gross_total += g
            n_trades += 1
            n_onesided += 1
            if g < 0:
                onesided_adverse += 1
            state = 0

    # Close remaining
    if state == 2:
        g = bid[-1] - inv_entry
        gross_pnl_total += g
        onesided_gross_total += g
        n_trades += 1
        n_onesided += 1
    elif state == 3:
        g = inv_entry - ask[-1]
        gross_pnl_total += g
        onesided_gross_total += g
        n_trades += 1
        n_onesided += 1

    net_pnl = gross_pnl_total - n_trades * RT_COST_PTS

    return {
        "day": day,
        "n_ticks": n,
        "n_opps": n_opps,
        "n_trades": n_trades,
        "n_both": n_both,
        "n_1s": n_onesided,
        "both_gross": round(both_gross_total, 1),
        "1s_gross": round(onesided_gross_total, 1),
        "gross": round(gross_pnl_total, 1),
        "net": round(net_pnl, 1),
        "net_ntd": round(net_pnl * POINT_VALUE_NTD, 0),
        "1s_adv_rate": round(onesided_adverse / max(n_onesided, 1), 3),
        "gr_per_rt": round(gross_pnl_total / max(n_trades, 1), 2),
    }


def main():
    all_files = sorted(DATA_DIR.glob("tmfd6_*.npz"))
    all_days = [f.stem.replace("tmfd6_", "") for f in all_files]
    print(f"Found {len(all_days)} days")

    is_days = [d for d in all_days if d < "2026-02-23"]
    oos_days = [d for d in all_days if d >= "2026-02-23"]

    # Regime analysis
    print("\n=== REGIME ANALYSIS ===")
    for day in all_days:
        data = load_day(day)
        if data is None:
            continue
        n = len(data["exch_ts"])
        spread = ((data["ask1_price"] - data["bid1_price"]) // PRICE_SCALE).astype(np.int64)
        med_sp = int(np.median(spread))
        pct5 = float(np.mean(spread >= 5)) * 100
        pct10 = float(np.mean(spread >= 10)) * 100
        avg_bv = float(np.mean(data["bid1_vol"]))
        regime = "WIDE" if med_sp >= 10 else ("MED" if med_sp >= 5 else "TIGHT")
        marker = "IS " if day in is_days else "OOS"
        print(f"  {day} [{marker}] {regime:>5}: n={n:>7}, med_spd={med_sp:>3}, "
              f">=5={pct5:>5.1f}%, >=10={pct10:>5.1f}%, avg_bid_vol={avg_bv:>5.1f}")

    # Backtest
    thresholds = [5, 8, 10, 15, 20, 30, 40]
    fill_models = ["L1", "L2"]

    print(f"\n{'FM':<3} {'Thr':>3} {'Per':<4}| {'Days':>3} {'RT':>6} {'Both':>5} {'1s':>5} "
          f"{'BothGr':>7} {'1sGr':>7} {'Gross':>8} {'Net':>8} {'Net/d':>7} {'NTD/d':>7} "
          f"{'Sharpe':>7} {'1sAdv%':>6} {'Gr/RT':>6}")
    print("-" * 110)

    all_summaries = []

    for fm in fill_models:
        for thr in thresholds:
            for pname, pdays in [("IS", is_days), ("OOS", oos_days), ("ALL", all_days)]:
                results = []
                for day in pdays:
                    r = simulate_day(day, thr, fm)
                    if r is not None and r["n_trades"] > 0:
                        results.append(r)

                if not results:
                    # Still print zero row
                    print(f"{fm:<3} {thr:>3} {pname:<4}| {len(pdays):>3} {0:>6} {0:>5} {0:>5} "
                          f"{0:>7} {0:>7} {0:>8} {0:>8} {0:>7} {0:>7} {0:>7.1f} {0:>5}% {0:>6.1f}")
                    continue

                nd = len(results)
                tot_rt = sum(r["n_trades"] for r in results)
                tot_both = sum(r["n_both"] for r in results)
                tot_1s = sum(r["n_1s"] for r in results)
                tot_bg = sum(r["both_gross"] for r in results)
                tot_1sg = sum(r["1s_gross"] for r in results)
                tot_gross = sum(r["gross"] for r in results)
                tot_net = sum(r["net"] for r in results)
                daily_nets = [r["net"] for r in results]
                avg_net = float(np.mean(daily_nets))
                std_net = float(np.std(daily_nets)) if nd > 1 else 1.0
                sharpe = avg_net / std_net * np.sqrt(252) if std_net > 0 else 0.0
                avg_adv = float(np.mean([r["1s_adv_rate"] for r in results]))
                avg_ntd = float(np.mean([r["net_ntd"] for r in results]))
                gr_rt = tot_gross / max(tot_rt, 1)

                row = {"fm": fm, "thr": thr, "period": pname, "nd": nd,
                       "rt": tot_rt, "both": tot_both, "1s": tot_1s,
                       "bg": tot_bg, "1sg": tot_1sg, "gross": tot_gross,
                       "net": tot_net, "net_d": round(avg_net, 1),
                       "ntd_d": round(avg_ntd, 0), "sharpe": round(sharpe, 2),
                       "adv": round(avg_adv * 100, 1), "gr_rt": round(gr_rt, 2)}
                all_summaries.append(row)

                print(f"{fm:<3} {thr:>3} {pname:<4}| {nd:>3} {tot_rt:>6} {tot_both:>5} {tot_1s:>5} "
                      f"{tot_bg:>7.0f} {tot_1sg:>7.0f} {tot_gross:>8.0f} {tot_net:>8.0f} "
                      f"{avg_net:>7.0f} {avg_ntd:>7.0f} {sharpe:>7.1f} {avg_adv*100:>5.0f}% {gr_rt:>6.1f}")

    # Per-day detail
    for fm, thr in [("L1", 5), ("L1", 10), ("L1", 20)]:
        print(f"\n=== PER-DAY: {fm} threshold={thr} ===")
        for day in all_days:
            r = simulate_day(day, thr, fm)
            if r:
                marker = "IS " if day in is_days else "OOS"
                print(f"  {day} [{marker}]: trades={r['n_trades']:>5} (both={r['n_both']:>4}, "
                      f"1s={r['n_1s']:>4}), opps={r['n_opps']:>6}, "
                      f"gross={r['gross']:>7.0f}, net={r['net']:>7.0f} ({r['net_ntd']:>7.0f} NTD), "
                      f"adv={r['1s_adv_rate']:.0%}, gr/rt={r['gr_per_rt']:>+.1f}")

    # Markout analysis
    print("\n=== MARKOUT ANALYSIS ===")
    print("When spread transitions from <5 to >=5, what happens to mid price?")
    for horizon_s in [0.5, 1.0, 5.0, 10.0]:
        horizon_ns = int(horizon_s * 1e9)
        all_markouts = []
        for day in all_days:
            data = load_day(day)
            if data is None:
                continue
            ts = data["exch_ts"]
            bid = data["bid1_price"].astype(np.float64) / PRICE_SCALE
            ask = data["ask1_price"].astype(np.float64) / PRICE_SCALE
            spread = ((data["ask1_price"] - data["bid1_price"]) // PRICE_SCALE).astype(np.int64)
            mid = (bid + ask) / 2.0
            n = len(ts)

            # Find transitions
            transitions = np.where((spread[:-1] < 5) & (spread[1:] >= 5))[0] + 1
            for idx in transitions:
                target_ts = ts[idx] + horizon_ns
                j = np.searchsorted(ts, target_ts)
                if j < n:
                    all_markouts.append(mid[j] - mid[idx])

        if all_markouts:
            arr = np.array(all_markouts)
            print(f"  {horizon_s}s: n={len(arr)}, mean={np.mean(arr):+.4f} pts, "
                  f"std={np.std(arr):.3f}, |mean|/std={abs(np.mean(arr))/np.std(arr):.4f}, "
                  f"adverse_bid={np.mean(arr < 0)*100:.1f}%, adverse_ask={np.mean(arr > 0)*100:.1f}%")

    # Save
    out = Path(__file__).parent / "results_v3.json"
    with open(out, "w") as f:
        json.dump(all_summaries, f, indent=2)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
