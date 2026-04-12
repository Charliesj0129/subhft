"""R47 Deep Analysis — max_pos impact, time-of-day, holding time, optimization.

Usage:
    uv run python research/tools/r47_deep_analysis.py
"""
from __future__ import annotations

import math
import os
from collections import defaultdict

import clickhouse_connect
import numpy as np

COMMISSION = 1.3
CK_SCALE = 1_000_000
TMFD6_PV = 10

DATES = [
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24",
    "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31",
    "2026-04-01", "2026-04-02", "2026-04-07", "2026-04-08",
]


def get_ch():
    return clickhouse_connect.get_client(
        host=os.getenv("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("HFT_CLICKHOUSE_USER", "default"),
        password=os.getenv("HFT_CLICKHOUSE_PASSWORD", "changeme"),
    )


def load_ba(client, symbol, date):
    r = client.query(f"""
        SELECT exch_ts, bids_price[1], bids_vol[1], asks_price[1], asks_vol[1]
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'BidAsk'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date}'
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
    """)
    rows = r.result_rows
    if not rows:
        return None
    return {
        "ts": np.array([x[0] for x in rows], dtype=np.int64),
        "bp": np.array([x[1] for x in rows], dtype=np.int64),
        "bv": np.array([x[2] for x in rows], dtype=np.int64),
        "ap": np.array([x[3] for x in rows], dtype=np.int64),
        "av": np.array([x[4] for x in rows], dtype=np.int64),
    }


def simulate_maker(exec_ba, spr_thresh, max_pos, skew_divisor=5):
    """Full maker simulation with inventory tracking.

    Returns list of fills with detailed metadata.
    """
    ts = exec_ba["ts"]
    bp = exec_ba["bp"]; bv = exec_ba["bv"]
    ap = exec_ba["ap"]; av = exec_ba["av"]
    n = len(ts)

    spread = (ap.astype(np.float64) - bp.astype(np.float64)) / CK_SCALE
    mid = (bp.astype(np.float64) + ap.astype(np.float64)) / 2.0

    pos = 0
    fills = []
    entry_prices = []  # track for realized PnL

    for i in range(1, n - 200):
        if spread[i] < spr_thresh:
            continue

        # Inventory skew: shift fair value to encourage closing
        spread_scaled = ap[i] - bp[i]
        tick_size = max(1, spread_scaled * 50 // 100)
        skew = -(pos * tick_size * 2) // skew_divisor

        # Our quotes (simplified: post at L1 with skew)
        our_bid = bp[i]  # + skew effect absorbed into position limit
        our_ask = ap[i]

        # Bid fill: queue hit (someone market-sells into our bid)
        can_bid = pos < max_pos
        bid_fill = (
            can_bid
            and bp[i] == bp[i - 1]
            and bv[i] < bv[i - 1]
            and bv[i - 1] > 0
        )

        # Ask fill: queue hit (someone market-buys into our ask)
        can_ask = pos > -max_pos
        ask_fill = (
            can_ask
            and ap[i] == ap[i - 1]
            and av[i] < av[i - 1]
            and av[i - 1] > 0
        )

        if bid_fill:
            pos += 1
            fill_px = float(bp[i]) / CK_SCALE

            # Forward PnL: measure at multiple horizons
            fwd_1s = np.searchsorted(ts, ts[i] + 1_000_000_000)
            fwd_5s = np.searchsorted(ts, ts[i] + 5_000_000_000)
            fwd_30s = np.searchsorted(ts, ts[i] + 30_000_000_000)

            mid_1s = mid[min(fwd_1s, n - 1)] / CK_SCALE if fwd_1s < n else fill_px
            mid_5s = mid[min(fwd_5s, n - 1)] / CK_SCALE if fwd_5s < n else fill_px
            mid_30s = mid[min(fwd_30s, n - 1)] / CK_SCALE if fwd_30s < n else fill_px

            # Extract hour (TWN = UTC+8)
            hour_utc = (ts[i] // 3_600_000_000_000) % 24
            hour_twn = (hour_utc + 8) % 24

            fills.append({
                "side": "bid", "px": fill_px, "spread": spread[i],
                "pos_after": pos, "ts": ts[i],
                "hour_twn": hour_twn,
                "pnl_1s": mid_1s - fill_px - COMMISSION,
                "pnl_5s": mid_5s - fill_px - COMMISSION,
                "pnl_30s": mid_30s - fill_px - COMMISSION,
                "pnl_gross_1s": mid_1s - fill_px,
                "l1_depth": int(bv[i - 1]),
            })

        if ask_fill:
            pos -= 1
            fill_px = float(ap[i]) / CK_SCALE

            fwd_1s = np.searchsorted(ts, ts[i] + 1_000_000_000)
            fwd_5s = np.searchsorted(ts, ts[i] + 5_000_000_000)
            fwd_30s = np.searchsorted(ts, ts[i] + 30_000_000_000)

            mid_1s = mid[min(fwd_1s, n - 1)] / CK_SCALE if fwd_1s < n else fill_px
            mid_5s = mid[min(fwd_5s, n - 1)] / CK_SCALE if fwd_5s < n else fill_px
            mid_30s = mid[min(fwd_30s, n - 1)] / CK_SCALE if fwd_30s < n else fill_px

            hour_utc = (ts[i] // 3_600_000_000_000) % 24
            hour_twn = (hour_utc + 8) % 24

            fills.append({
                "side": "ask", "px": fill_px, "spread": spread[i],
                "pos_after": pos, "ts": ts[i],
                "hour_twn": hour_twn,
                "pnl_1s": fill_px - mid_1s - COMMISSION,
                "pnl_5s": fill_px - mid_5s - COMMISSION,
                "pnl_30s": fill_px - mid_30s - COMMISSION,
                "pnl_gross_1s": fill_px - mid_1s,
                "l1_depth": int(av[i - 1]),
            })

    return fills, pos


def main():
    print("=" * 80, flush=True)
    print("R47 Deep Analysis — Position Limits, Time-of-Day, Optimization", flush=True)
    print(f"Commission: {COMMISSION} pts/side | 12 days TMFD6", flush=True)
    print("=" * 80, flush=True)

    client = get_ch()

    # Load all TMFD6 data
    print("\nLoading TMFD6 data...", flush=True)
    day_data = []
    for date in DATES:
        ba = load_ba(client, "TMFD6", date)
        if ba and len(ba["ts"]) > 1000:
            day_data.append({"date": date, "ba": ba})
            print(f"  {date}: {len(ba['ts']):,} BA", flush=True)

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS 1: max_pos impact (1, 2, 3, 5, 10)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80, flush=True)
    print("ANALYSIS 1: MAX POSITION IMPACT", flush=True)
    print("=" * 80, flush=True)

    for spr in [3, 4, 5]:
        print(f"\n  spread_threshold = {spr} pts:", flush=True)
        print(f"  {'MaxPos':>6} {'Fills':>8} {'AvgNet':>8} {'Win%':>6} "
              f"{'TotNet':>10} {'NTD':>10} {'NTD/day':>10} "
              f"{'MaxDD':>8} {'Sharpe':>8}", flush=True)
        print(f"  {'-' * 82}", flush=True)

        for mp in [1, 2, 3, 5, 10]:
            all_fills = []
            day_pnls = []

            for dd in day_data:
                fills, final_pos = simulate_maker(dd["ba"], spr, mp)
                day_net = sum(f["pnl_1s"] for f in fills)
                all_fills.extend(fills)
                day_pnls.append(day_net)

            if not all_fills:
                print(f"  {mp:>6} {'no fills':>8}", flush=True)
                continue

            pnls = np.array([f["pnl_1s"] for f in all_fills])
            total = float(np.sum(pnls))
            avg = float(np.mean(pnls))
            win = float(np.mean(pnls > 0) * 100)
            ntd = total * TMFD6_PV
            ntd_day = ntd / len(day_data)

            # Max drawdown from daily PnLs
            cum = np.cumsum(day_pnls)
            peak = np.maximum.accumulate(cum)
            dd_arr = peak - cum
            max_dd = float(np.max(dd_arr)) if len(dd_arr) > 0 else 0

            # Sharpe
            if np.std(day_pnls) > 0:
                sharpe = np.mean(day_pnls) / np.std(day_pnls) * math.sqrt(252)
            else:
                sharpe = 0

            print(f"  {mp:>6} {len(all_fills):>8,} {avg:>+8.2f} {win:>5.1f}% "
                  f"{total:>+10.0f} {ntd:>+10,.0f} {ntd_day:>+10,.0f} "
                  f"{max_dd:>8.0f} {sharpe:>+8.1f}", flush=True)

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS 2: Time-of-Day profitability (spread=4, max_pos=2)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80, flush=True)
    print("ANALYSIS 2: TIME-OF-DAY (spread=4, max_pos=2)", flush=True)
    print("=" * 80, flush=True)

    all_fills_tod = []
    for dd in day_data:
        fills, _ = simulate_maker(dd["ba"], 4, 2)
        all_fills_tod.extend(fills)

    hour_stats = defaultdict(list)
    for f in all_fills_tod:
        hour_stats[f["hour_twn"]].append(f["pnl_1s"])

    print(f"\n  {'Hour(TWN)':>10} {'Fills':>8} {'AvgNet':>8} {'Win%':>6} {'TotNet':>10}", flush=True)
    print(f"  {'-' * 46}", flush=True)
    for h in sorted(hour_stats.keys()):
        pnls = np.array(hour_stats[h])
        if len(pnls) >= 10:
            print(f"  {h:>8}:00 {len(pnls):>8,} {np.mean(pnls):>+8.2f} "
                  f"{np.mean(pnls > 0)*100:>5.1f}% {np.sum(pnls):>+10.1f}", flush=True)

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS 3: Forward horizon impact (1s vs 5s vs 30s)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80, flush=True)
    print("ANALYSIS 3: FORWARD HORIZON (spread=4, max_pos=2)", flush=True)
    print("=" * 80, flush=True)

    pnl_1s = np.array([f["pnl_1s"] for f in all_fills_tod])
    pnl_5s = np.array([f["pnl_5s"] for f in all_fills_tod])
    pnl_30s = np.array([f["pnl_30s"] for f in all_fills_tod])

    print(f"\n  {'Horizon':>10} {'AvgNet':>8} {'Win%':>6} {'TotNet':>10} {'NTD':>10}", flush=True)
    print(f"  {'-' * 48}", flush=True)
    for label, arr in [("1s", pnl_1s), ("5s", pnl_5s), ("30s", pnl_30s)]:
        print(f"  {label:>10} {np.mean(arr):>+8.2f} {np.mean(arr > 0)*100:>5.1f}% "
              f"{np.sum(arr):>+10.0f} {np.sum(arr)*TMFD6_PV:>+10,.0f}", flush=True)

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS 4: L1 Depth at fill — does thinner book = worse fills?
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80, flush=True)
    print("ANALYSIS 4: L1 DEPTH AT FILL (spread=4, max_pos=2)", flush=True)
    print("=" * 80, flush=True)

    depth_stats = defaultdict(list)
    for f in all_fills_tod:
        d = min(f["l1_depth"], 10)  # cap at 10
        depth_stats[d].append(f["pnl_1s"])

    print(f"\n  {'L1 Depth':>10} {'Fills':>8} {'AvgNet':>8} {'Win%':>6} {'AvgGross':>9}", flush=True)
    print(f"  {'-' * 48}", flush=True)
    for d in sorted(depth_stats.keys()):
        pnls = np.array(depth_stats[d])
        gross = np.array([f["pnl_gross_1s"] for f in all_fills_tod if min(f["l1_depth"], 10) == d])
        if len(pnls) >= 10:
            print(f"  {d:>8} lots {len(pnls):>8,} {np.mean(pnls):>+8.2f} "
                  f"{np.mean(pnls > 0)*100:>5.1f}% {np.mean(gross):>+9.2f}", flush=True)

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS 5: Spread bucket profitability
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80, flush=True)
    print("ANALYSIS 5: SPREAD BUCKET (max_pos=2)", flush=True)
    print("=" * 80, flush=True)

    # Use all fills with no spread filter
    all_fills_raw = []
    for dd in day_data:
        fills, _ = simulate_maker(dd["ba"], 1, 2)
        all_fills_raw.extend(fills)

    spr_stats = defaultdict(list)
    for f in all_fills_raw:
        s = int(f["spread"])
        spr_stats[s].append(f["pnl_1s"])

    print(f"\n  {'Spread':>8} {'Fills':>8} {'AvgGross':>9} {'AvgNet':>8} "
          f"{'Win%':>6} {'TotNet':>10} {'NTD':>10}", flush=True)
    print(f"  {'-' * 65}", flush=True)
    for s in sorted(spr_stats.keys()):
        pnls = np.array(spr_stats[s])
        if len(pnls) >= 10:
            gross = np.mean(pnls) + COMMISSION
            print(f"  {s:>7}pt {len(pnls):>8,} {gross:>+9.2f} {np.mean(pnls):>+8.2f} "
                  f"{np.mean(pnls > 0)*100:>5.1f}% {np.sum(pnls):>+10.0f} "
                  f"{np.sum(pnls)*TMFD6_PV:>+10,.0f}", flush=True)

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS 6: Inventory skew divisor optimization
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80, flush=True)
    print("ANALYSIS 6: INVENTORY SKEW DIVISOR (spread=4, max_pos=2)", flush=True)
    print("=" * 80, flush=True)

    print(f"\n  {'Divisor':>8} {'Fills':>8} {'AvgNet':>8} {'Win%':>6} "
          f"{'TotNet':>10} {'NTD':>10}", flush=True)
    print(f"  {'-' * 54}", flush=True)
    for div in [2, 3, 5, 8, 10, 20, 100]:
        all_f = []
        for dd in day_data:
            fills, _ = simulate_maker(dd["ba"], 4, 2, skew_divisor=div)
            all_f.extend(fills)
        if all_f:
            pnls = np.array([f["pnl_1s"] for f in all_f])
            print(f"  {div:>8} {len(all_f):>8,} {np.mean(pnls):>+8.2f} "
                  f"{np.mean(pnls > 0)*100:>5.1f}% {np.sum(pnls):>+10.0f} "
                  f"{np.sum(pnls)*TMFD6_PV:>+10,.0f}", flush=True)

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS 7: Per-day consistency (spread=4, max_pos=1 vs 2)
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80, flush=True)
    print("ANALYSIS 7: PER-DAY CONSISTENCY (spread=4)", flush=True)
    print("=" * 80, flush=True)

    for mp in [1, 2]:
        print(f"\n  max_pos = {mp}:", flush=True)
        print(f"  {'Date':>12} {'Fills':>7} {'Net':>+10} {'NTD':>+10} {'Win%':>6} "
              f"{'MaxPos':>7} {'AvgPos':>7}", flush=True)
        print(f"  {'-' * 62}", flush=True)

        total_net = 0
        for dd in day_data:
            fills, final_pos = simulate_maker(dd["ba"], 4, mp)
            if not fills:
                print(f"  {dd['date']:>12} {'no fills':>7}", flush=True)
                continue
            pnls = np.array([f["pnl_1s"] for f in fills])
            positions = [f["pos_after"] for f in fills]
            day_net = float(np.sum(pnls))
            total_net += day_net
            marker = "✅" if day_net > 0 else "❌"
            print(f"  {marker}{dd['date']:>11} {len(fills):>7,} {day_net:>+10.1f} "
                  f"{day_net * TMFD6_PV:>+10,.0f} {np.mean(pnls > 0)*100:>5.1f}% "
                  f"{max(abs(p) for p in positions):>7} "
                  f"{np.mean(np.abs(positions)):>7.1f}", flush=True)

        print(f"  {'TOTAL':>13} {'':>7} {total_net:>+10.0f} "
              f"{total_net * TMFD6_PV:>+10,.0f}", flush=True)

    # ═══════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80, flush=True)
    print("OPTIMIZATION SUMMARY", flush=True)
    print("=" * 80, flush=True)


if __name__ == "__main__":
    main()
