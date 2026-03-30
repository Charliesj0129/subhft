"""
Fast OpMM Backtest on TMFD6 using numba JIT.

Fill model:
  - Post at best bid/ask when spread >= threshold
  - BID FILL: bid drops below posted level (aggressive seller took our level)
  - ASK FILL: ask rises above posted level (aggressive buyer took our level)
  - After one-sided fill, close at market on next tick
  - Both-fill: both sides move simultaneously -> earn full spread

RT cost = 4 points (40 NTD), Point value = 10 NTD/point
"""
from __future__ import annotations
import numpy as np
import numba as nb
from pathlib import Path
import json
import time

PRICE_SCALE = 1_000_000
RT_COST_PTS = 4
POINT_VALUE_NTD = 10
DATA_DIR = Path(__file__).parent / "data"


@nb.njit
def simulate_core(bid: np.ndarray, ask: np.ndarray, ts: np.ndarray,
                  threshold: float, strict: bool) -> tuple:
    """
    Returns: (n_trades, n_both, n_1s, both_gross, onesided_gross,
              onesided_adverse, n_opps, trade_pnls_array)
    """
    n = len(bid)
    # Pre-allocate for trade PnLs (max possible = n/2)
    max_trades = n // 2
    trade_pnls = np.empty(max_trades, dtype=np.float64)

    state = 0  # 0=FLAT, 1=QUOTING, 2=LONG, 3=SHORT
    posted_bid = 0.0
    posted_ask = 0.0

    inv_entry = 0.0
    n_trades = 0
    n_both = 0
    n_1s = 0
    both_gross = 0.0
    onesided_gross = 0.0
    onesided_adverse = 0
    n_opps = 0

    offset = 1.0 if strict else 0.0

    for i in range(1, n):
        cb = bid[i]
        ca = ask[i]
        cs = ca - cb

        if state == 0:
            if cs >= threshold:
                n_opps += 1
                posted_bid = cb
                posted_ask = ca
                state = 1

        elif state == 1:
            bf = cb < posted_bid - offset
            af = ca > posted_ask + offset

            if bf and af:
                g = posted_ask - posted_bid
                both_gross += g
                if n_trades < max_trades:
                    trade_pnls[n_trades] = g
                n_trades += 1
                n_both += 1
                state = 0
            elif bf:
                inv_entry = posted_bid
                state = 2
            elif af:
                inv_entry = posted_ask
                state = 3
            elif cs < threshold or cb > posted_bid + 1.0 or ca < posted_ask - 1.0:
                state = 0

        elif state == 2:
            g = cb - inv_entry
            onesided_gross += g
            if g < 0:
                onesided_adverse += 1
            if n_trades < max_trades:
                trade_pnls[n_trades] = g
            n_trades += 1
            n_1s += 1
            state = 0

        elif state == 3:
            g = inv_entry - ca
            onesided_gross += g
            if g < 0:
                onesided_adverse += 1
            if n_trades < max_trades:
                trade_pnls[n_trades] = g
            n_trades += 1
            n_1s += 1
            state = 0

    return (n_trades, n_both, n_1s, both_gross, onesided_gross,
            onesided_adverse, n_opps, trade_pnls[:min(n_trades, max_trades)])


def load_day(day: str) -> dict[str, np.ndarray] | None:
    fpath = DATA_DIR / f"tmfd6_{day}.npz"
    if not fpath.exists():
        return None
    data = np.load(fpath)
    return {k: data[k] for k in data.files}


def simulate_day(day: str, threshold_pts: int, fill_model: str) -> dict[str, float] | None:
    data = load_day(day)
    if data is None:
        return None

    ts = data["exch_ts"]
    bid = data["bid1_price"].astype(np.float64) / PRICE_SCALE
    ask = data["ask1_price"].astype(np.float64) / PRICE_SCALE
    n = len(ts)
    if n < 100:
        return None

    strict = fill_model == "L2"
    (n_trades, n_both, n_1s, both_gross, os_gross,
     os_adverse, n_opps, trade_pnls) = simulate_core(
        bid, ask, ts, float(threshold_pts), strict
    )

    gross = both_gross + os_gross
    net = gross - n_trades * RT_COST_PTS

    # Max drawdown from trade PnLs
    if len(trade_pnls) > 0:
        cumsum = np.cumsum(trade_pnls - RT_COST_PTS)
        peak = np.maximum.accumulate(cumsum)
        max_dd = float(np.max(peak - cumsum))
    else:
        max_dd = 0.0

    return {
        "day": day,
        "n_ticks": n,
        "n_opps": n_opps,
        "n_trades": n_trades,
        "n_both": n_both,
        "n_1s": n_1s,
        "both_gross": round(both_gross, 1),
        "1s_gross": round(os_gross, 1),
        "1s_adv_rate": round(os_adverse / max(n_1s, 1), 3),
        "gross": round(gross, 1),
        "net": round(net, 1),
        "net_ntd": round(net * POINT_VALUE_NTD, 0),
        "gr_per_rt": round(gross / max(n_trades, 1), 2),
        "max_dd": round(max_dd, 1),
    }


def markout_analysis(all_days: list[str]) -> None:
    """Analyze mid-price movement after spread widens."""
    print("\n=== MARKOUT ANALYSIS ===")
    print("When spread transitions from <5 to >=5, mid-price change after N seconds:")

    for horizon_s in [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]:
        horizon_ns = int(horizon_s * 1e9)
        markouts: list[float] = []
        for day in all_days:
            data = load_day(day)
            if data is None:
                continue
            ts = data["exch_ts"]
            bid = data["bid1_price"].astype(np.float64) / PRICE_SCALE
            ask = data["ask1_price"].astype(np.float64) / PRICE_SCALE
            spread_int = ((data["ask1_price"] - data["bid1_price"]) // PRICE_SCALE).astype(np.int64)
            mid = (bid + ask) / 2.0

            # Vectorized transition detection
            trans = np.where((spread_int[:-1] < 5) & (spread_int[1:] >= 5))[0] + 1
            for idx in trans:
                j = np.searchsorted(ts, ts[idx] + horizon_ns)
                if j < len(ts):
                    markouts.append(mid[j] - mid[idx])

        if markouts:
            arr = np.array(markouts)
            print(f"  {horizon_s:>5.1f}s: n={len(arr):>6}, mean={np.mean(arr):+.4f}, "
                  f"std={np.std(arr):.3f}, t-stat={np.mean(arr)/np.std(arr)*np.sqrt(len(arr)):.2f}, "
                  f"bid_adv={np.mean(arr<0)*100:.1f}%, ask_adv={np.mean(arr>0)*100:.1f}%")


def main():
    t0 = time.time()
    all_files = sorted(DATA_DIR.glob("tmfd6_*.npz"))
    all_days = [f.stem.replace("tmfd6_", "") for f in all_files]
    print(f"Found {len(all_days)} days of data")

    is_days = [d for d in all_days if d < "2026-02-23"]
    oos_days = [d for d in all_days if d >= "2026-02-23"]
    print(f"IS: {len(is_days)} days ({is_days[0]}..{is_days[-1]})")
    print(f"OOS: {len(oos_days)} days ({oos_days[0]}..{oos_days[-1]})")

    # Warm up numba
    print("Warming up JIT...")
    _ = simulate_day(all_days[0], 10, "L1")
    print(f"JIT warmup done ({time.time()-t0:.1f}s)")

    # Regime analysis
    print("\n=== REGIME ANALYSIS ===")
    for day in all_days:
        data = load_day(day)
        if data is None:
            continue
        n = len(data["exch_ts"])
        sp = ((data["ask1_price"] - data["bid1_price"]) // PRICE_SCALE).astype(np.int64)
        med = int(np.median(sp))
        p5 = float(np.mean(sp >= 5) * 100)
        p10 = float(np.mean(sp >= 10) * 100)
        bv = float(np.mean(data["bid1_vol"]))
        regime = "WIDE" if med >= 10 else ("MED" if med >= 5 else "TIGHT")
        mk = "IS " if day in is_days else "OOS"
        print(f"  {day} [{mk}] {regime:>5}: n={n:>7}, med={med:>3}, >=5={p5:>5.1f}%, >=10={p10:>5.1f}%, bv={bv:>5.1f}")

    # Main backtest
    thresholds = [5, 8, 10, 15, 20, 30, 40]
    fill_models = ["L1", "L2"]

    print(f"\n{'FM':<3} {'Thr':>3} {'Per':<4}| {'D':>2} {'RT':>6} {'Both':>5} {'1s':>5} "
          f"{'BothGr':>7} {'1sGr':>7} {'Gross':>8} {'Net':>8} {'Net/d':>7} {'NTD/d':>7} "
          f"{'Sharpe':>7} {'1sAdv':>5} {'Gr/RT':>6}")
    print("-" * 108)

    all_summaries: list[dict] = []

    for fm in fill_models:
        for thr in thresholds:
            for pname, pdays in [("IS", is_days), ("OOS", oos_days), ("ALL", all_days)]:
                results = []
                for day in pdays:
                    r = simulate_day(day, thr, fm)
                    if r is not None:
                        results.append(r)

                if not results:
                    continue

                # Only show days with trades for aggregation
                active = [r for r in results if r["n_trades"] > 0]
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
                avg_adv = float(np.mean([r["1s_adv_rate"] for r in active])) if active else 0.0
                avg_ntd = float(np.mean([r["net_ntd"] for r in results]))
                gr_rt = tot_gross / max(tot_rt, 1)

                row = {"fm": fm, "thr": thr, "period": pname, "nd": nd,
                       "rt": tot_rt, "both": tot_both, "1s": tot_1s,
                       "bg": tot_bg, "1sg": tot_1sg, "gross": tot_gross,
                       "net": tot_net, "net_d": round(avg_net, 1),
                       "ntd_d": round(avg_ntd, 0), "sharpe": round(sharpe, 2),
                       "adv": round(avg_adv * 100, 1), "gr_rt": round(gr_rt, 2)}
                all_summaries.append(row)

                print(f"{fm:<3} {thr:>3} {pname:<4}| {nd:>2} {tot_rt:>6} {tot_both:>5} {tot_1s:>5} "
                      f"{tot_bg:>7.0f} {tot_1sg:>7.0f} {tot_gross:>8.0f} {tot_net:>8.0f} "
                      f"{avg_net:>7.0f} {avg_ntd:>7.0f} {sharpe:>7.1f} {avg_adv*100:>5.0f}% {gr_rt:>6.1f}")

    # Per-day detail
    for fm, thr in [("L1", 5), ("L1", 10), ("L1", 20), ("L1", 30)]:
        print(f"\n=== PER-DAY: {fm} threshold={thr} ===")
        for day in all_days:
            r = simulate_day(day, thr, fm)
            if r:
                mk = "IS " if day in is_days else "OOS"
                s = f"  {day} [{mk}]: trades={r['n_trades']:>5} (both={r['n_both']:>4}, 1s={r['n_1s']:>4})"
                s += f", opps={r['n_opps']:>6}, gross={r['gross']:>7.0f}, net={r['net']:>7.0f}"
                s += f" ({r['net_ntd']:>7.0f} NTD), adv={r['1s_adv_rate']:.0%}, gr/rt={r['gr_per_rt']:>+.1f}"
                print(s)

    # Spread distribution text histograms
    print("\n=== SPREAD DISTRIBUTION ===")
    for sample_day in ["2026-01-30", "2026-02-24", "2026-03-23"]:
        data = load_day(sample_day)
        if data is None:
            continue
        sp = ((data["ask1_price"] - data["bid1_price"]) // PRICE_SCALE).astype(np.int64)
        print(f"\n  {sample_day} (n={len(sp)}):")
        for b in range(1, 51):
            pct = float(np.mean(sp == b) * 100)
            if pct >= 0.3:
                bar = "#" * int(pct * 2)
                print(f"    {b:>3}: {pct:>5.1f}% {bar}")
        rest = float(np.mean(sp > 50) * 100)
        if rest > 0.5:
            print(f"    >50: {rest:>5.1f}%")

    # Markout
    markout_analysis(all_days)

    # TXFD6 comparison if available
    print("\n=== TXFD6 COMPARISON ===")
    txf_files = sorted(Path("research/experiments/validations/tmfd6_opmm/data").glob("txfd6_*.npz"))
    if not txf_files:
        # Try to load TXFD6 from same ClickHouse but skip if not pre-extracted
        print("  TXFD6 data not pre-extracted. Skipping comparison.")
        print("  (For comparison: TXFD6 has 2.1% time at spread >= 2, RT cost ~35 NTD = 0.7 pts)")

    elapsed = time.time() - t0
    print(f"\nTotal time: {elapsed:.1f}s")

    # Save
    out = Path(__file__).parent / "results_final.json"
    with open(out, "w") as f:
        json.dump(all_summaries, f, indent=2)
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
