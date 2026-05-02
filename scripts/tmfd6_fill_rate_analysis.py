#!/usr/bin/env python3
"""
TMFD6 (微台期) Empirical Fill-Rate & Gross-Edge Analysis
=========================================================
Queries ClickHouse directly for BidAsk data and computes:
1. Spread distribution per day
2. L1 queue depth
3. Fill opportunity rate (queue depletion)
4. Adverse selection per fill opportunity
5. Gross edge per RT
6. Net edge after 4.0 pts RT cost
7. Break-even commission analysis
8. Volume-building analysis
"""

import os
import sys
import urllib.request
import urllib.parse
import json
import math
from collections import defaultdict

CH_HOST = "localhost"
CH_PORT = 8123
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
SYMBOL = "TMFD6"
SCALE = 1_000_000  # CK prices are x1,000,000

# Cost structure (confirmed by user)
POINT_VALUE_NTD = 10
COMMISSION_PER_SIDE_PTS = 1.3  # 13 NTD
TAX_PER_SIDE_PTS = 0.7         # 7 NTD
TOTAL_PER_SIDE_PTS = 2.0       # 20 NTD
RT_COST_PTS = 4.0              # 40 NTD


def ch_query(sql: str) -> str:
    """Execute a ClickHouse query via HTTP and return result as string."""
    url = f"http://{CH_HOST}:{CH_PORT}/"
    params = {
        "query": sql,
        "user": "default",
        "password": CH_PASSWORD,
    }
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as e:
        print(f"ERROR querying ClickHouse: {e}")
        sys.exit(1)


def ch_query_json(sql: str) -> list[dict]:
    """Execute query and return as list of dicts (JSONEachRow)."""
    result = ch_query(sql + " FORMAT JSONEachRow")
    if not result:
        return []
    rows = []
    for line in result.split("\n"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def section(title: str):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")


def main():
    print("TMFD6 Fill-Rate & Gross-Edge Analysis")
    print("=" * 80)

    # ----- Check data availability -----
    section("0. Data Availability")
    rows = ch_query_json(f"""
        SELECT
            toDate(toDateTime(exch_ts / 1000000000)) AS day,
            count() AS cnt,
            countIf(type = 'BidAsk') AS bidask_cnt,
            countIf(type = 'Tick') AS tick_cnt
        FROM hft.market_data
        WHERE symbol = '{SYMBOL}'
        GROUP BY day
        ORDER BY day
    """)
    if not rows:
        print(f"ERROR: No data found for symbol '{SYMBOL}' in hft.market_data")
        # Try alternative symbol patterns
        alt = ch_query(f"SELECT DISTINCT symbol FROM hft.market_data WHERE symbol LIKE '%TMF%' OR symbol LIKE '%tmf%' LIMIT 20")
        print(f"Available TMF-like symbols: {alt}")
        alt2 = ch_query(f"SELECT DISTINCT symbol FROM hft.market_data LIMIT 50")
        print(f"All available symbols (first 50): {alt2}")
        return

    total_bidask = 0
    for r in rows:
        d = r["day"]
        ba = int(r["bidask_cnt"])
        tk = int(r["tick_cnt"])
        total_bidask += ba
        print(f"  {d}: {ba:>10,} BidAsk, {tk:>10,} Tick")
    print(f"  TOTAL: {total_bidask:>10,} BidAsk snapshots")

    if total_bidask == 0:
        print("ERROR: No BidAsk data available. Cannot proceed.")
        return

    # ----- 1. Spread Distribution -----
    section("1. Spread Distribution (points)")
    # Spread = (asks_price[1] - bids_price[1]) / 1e6
    spread_rows = ch_query_json(f"""
        SELECT
            toDate(toDateTime(exch_ts / 1000000000)) AS day,
            count() AS n,
            avg(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS avg_spread,
            median(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS med_spread,
            min(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS min_spread,
            max(toFloat64(asks_price[1] - bids_price[1]) / {SCALE}) AS max_spread,
            countIf(asks_price[1] - bids_price[1] >= 3 * {SCALE}) * 100.0 / count() AS pct_gte3,
            countIf(asks_price[1] - bids_price[1] >= 4 * {SCALE}) * 100.0 / count() AS pct_gte4,
            countIf(asks_price[1] - bids_price[1] >= 5 * {SCALE}) * 100.0 / count() AS pct_gte5
        FROM hft.market_data
        WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
          AND length(bids_price) >= 1 AND length(asks_price) >= 1
          AND bids_price[1] > 0 AND asks_price[1] > 0
        GROUP BY day
        ORDER BY day
    """)
    print(f"  {'Day':<12} {'N':>8} {'Avg':>7} {'Med':>7} {'Min':>6} {'Max':>6} {'>=3':>6} {'>=4':>6} {'>=5':>6}")
    print(f"  {'-'*12} {'-'*8} {'-'*7} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")
    total_n = 0
    weighted_avg = 0.0
    for r in spread_rows:
        n = int(r["n"])
        total_n += n
        weighted_avg += float(r["avg_spread"]) * n
        print(f"  {r['day']:<12} {n:>8,} {float(r['avg_spread']):>7.2f} {float(r['med_spread']):>7.2f} "
              f"{float(r['min_spread']):>6.1f} {float(r['max_spread']):>6.1f} "
              f"{float(r['pct_gte3']):>5.1f}% {float(r['pct_gte4']):>5.1f}% {float(r['pct_gte5']):>5.1f}%")
    if total_n > 0:
        overall_avg_spread = weighted_avg / total_n
        print(f"\n  Overall avg spread: {overall_avg_spread:.2f} pts")
        print(f"  Half-spread (maker capture): {overall_avg_spread / 2:.2f} pts")

    # ----- 2. L1 Queue Depth -----
    section("2. L1 Queue Depth")
    depth_rows = ch_query_json(f"""
        SELECT
            toDate(toDateTime(exch_ts / 1000000000)) AS day,
            avg(bids_vol[1]) AS avg_bid_vol,
            avg(asks_vol[1]) AS avg_ask_vol,
            median(bids_vol[1]) AS med_bid_vol,
            median(asks_vol[1]) AS med_ask_vol,
            max(bids_vol[1]) AS max_bid_vol,
            max(asks_vol[1]) AS max_ask_vol
        FROM hft.market_data
        WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
          AND length(bids_vol) >= 1 AND length(asks_vol) >= 1
        GROUP BY day
        ORDER BY day
    """)
    print(f"  {'Day':<12} {'AvgBid':>8} {'AvgAsk':>8} {'MedBid':>8} {'MedAsk':>8} {'MaxBid':>8} {'MaxAsk':>8}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for r in depth_rows:
        print(f"  {r['day']:<12} {float(r['avg_bid_vol']):>8.1f} {float(r['avg_ask_vol']):>8.1f} "
              f"{float(r['med_bid_vol']):>8.0f} {float(r['med_ask_vol']):>8.0f} "
              f"{int(r['max_bid_vol']):>8,} {int(r['max_ask_vol']):>8,}")

    # ----- 3-5. Fill Opportunity, Adverse Selection, Gross Edge -----
    # This requires sequential analysis per day -- pull raw BidAsk data and analyze in Python
    section("3-5. Fill Opportunities, Adverse Selection, Gross Edge")
    print("  Processing per-day BidAsk sequences (this may take a moment)...")

    all_days_results = []

    # Get list of days
    day_list = [r["day"] for r in rows if int(r["bidask_cnt"]) > 0]

    for day in day_list:
        # Pull BidAsk data for this day, ordered by exch_ts
        ba_data = ch_query_json(f"""
            SELECT
                exch_ts,
                bids_price[1] AS bp1,
                asks_price[1] AS ap1,
                bids_vol[1] AS bv1,
                asks_vol[1] AS av1
            FROM hft.market_data
            WHERE symbol = '{SYMBOL}' AND type = 'BidAsk'
              AND toDate(toDateTime(exch_ts / 1000000000)) = '{day}'
              AND length(bids_price) >= 1 AND length(asks_price) >= 1
              AND bids_price[1] > 0 AND asks_price[1] > 0
            ORDER BY exch_ts
        """)

        if len(ba_data) < 20:
            print(f"  {day}: only {len(ba_data)} snapshots, skipping")
            continue

        # Convert to lists for fast processing
        n = len(ba_data)
        ts_list = [int(r["exch_ts"]) for r in ba_data]
        bp1 = [int(r["bp1"]) for r in ba_data]
        ap1 = [int(r["ap1"]) for r in ba_data]
        bv1 = [int(r["bv1"]) for r in ba_data]
        av1 = [int(r["av1"]) for r in ba_data]
        mid = [(bp1[i] + ap1[i]) / 2.0 for i in range(n)]

        # Fill opportunities: consecutive snapshots where price unchanged but vol decreases
        bid_fill_opps = []  # indices where bid queue depleted (potential buy fill)
        ask_fill_opps = []  # indices where ask queue depleted (potential sell fill)

        for i in range(1, n):
            # Bid side: same bid price, bid vol decreased
            if bp1[i] == bp1[i-1] and bv1[i] < bv1[i-1] and bv1[i-1] > 0:
                bid_fill_opps.append(i)
            # Ask side: same ask price, ask vol decreased
            if ap1[i] == ap1[i-1] and av1[i] < av1[i-1] and av1[i-1] > 0:
                ask_fill_opps.append(i)

        total_fill_opps = len(bid_fill_opps) + len(ask_fill_opps)

        # Adverse selection: mid-price change after fill opportunity
        # For bid fills (we bought): adverse = mid went DOWN after fill
        # For ask fills (we sold): adverse = mid went UP after fill
        # Convention: adverse move is positive = bad for us
        adv_1 = []
        adv_5 = []
        adv_10 = []

        for idx in bid_fill_opps:
            fill_mid = mid[idx]
            for offset, adv_list in [(1, adv_1), (5, adv_5), (10, adv_10)]:
                if idx + offset < n:
                    future_mid = mid[idx + offset]
                    # We bought at bid. Adverse = price went down
                    adverse_move = (fill_mid - future_mid) / SCALE  # in points
                    adv_list.append(adverse_move)

        for idx in ask_fill_opps:
            fill_mid = mid[idx]
            for offset, adv_list in [(1, adv_1), (5, adv_5), (10, adv_10)]:
                if idx + offset < n:
                    future_mid = mid[idx + offset]
                    # We sold at ask. Adverse = price went up
                    adverse_move = (future_mid - fill_mid) / SCALE  # in points
                    adv_list.append(adverse_move)

        avg_adv_1 = sum(adv_1) / len(adv_1) if adv_1 else 0
        avg_adv_5 = sum(adv_5) / len(adv_5) if adv_5 else 0
        avg_adv_10 = sum(adv_10) / len(adv_10) if adv_10 else 0

        # Spread captured (half-spread at time of fill)
        spread_at_fills = []
        for idx in bid_fill_opps + ask_fill_opps:
            spread_pts = (ap1[idx] - bp1[idx]) / SCALE
            spread_at_fills.append(spread_pts)
        avg_spread_at_fill = sum(spread_at_fills) / len(spread_at_fills) if spread_at_fills else 0
        half_spread = avg_spread_at_fill / 2.0

        # Gross edge = half_spread - adverse_selection (using 5-snapshot adverse)
        gross_edge = half_spread - avg_adv_5
        # Net edge = gross - RT cost
        net_edge = gross_edge * 2 - RT_COST_PTS  # *2 because gross_edge is per side, RT is round trip

        result = {
            "day": day,
            "n_snapshots": n,
            "bid_fill_opps": len(bid_fill_opps),
            "ask_fill_opps": len(ask_fill_opps),
            "total_fill_opps": total_fill_opps,
            "avg_adv_1": avg_adv_1,
            "avg_adv_5": avg_adv_5,
            "avg_adv_10": avg_adv_10,
            "avg_spread_at_fill": avg_spread_at_fill,
            "half_spread": half_spread,
            "gross_edge_per_side": gross_edge,
            "gross_edge_per_rt": gross_edge * 2,
            "net_edge_per_rt": net_edge,
        }
        all_days_results.append(result)

    # Print fill opportunity results
    print(f"\n  {'Day':<12} {'Snaps':>8} {'BidFill':>8} {'AskFill':>8} {'Total':>8} {'Rate%':>7}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*7}")
    for r in all_days_results:
        rate = r["total_fill_opps"] / r["n_snapshots"] * 100 if r["n_snapshots"] else 0
        print(f"  {r['day']:<12} {r['n_snapshots']:>8,} {r['bid_fill_opps']:>8,} {r['ask_fill_opps']:>8,} "
              f"{r['total_fill_opps']:>8,} {rate:>6.1f}%")

    section("4. Adverse Selection (points, positive = unfavorable)")
    print(f"  {'Day':<12} {'Adv@1':>8} {'Adv@5':>8} {'Adv@10':>8}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8}")
    for r in all_days_results:
        print(f"  {r['day']:<12} {r['avg_adv_1']:>8.3f} {r['avg_adv_5']:>8.3f} {r['avg_adv_10']:>8.3f}")

    section("5. Gross Edge Analysis (points)")
    print(f"  {'Day':<12} {'Spread':>8} {'HlfSprd':>8} {'Adv@5':>8} {'Gross/s':>8} {'Gross/RT':>9} {'Net/RT':>8}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*9} {'-'*8}")
    for r in all_days_results:
        print(f"  {r['day']:<12} {r['avg_spread_at_fill']:>8.2f} {r['half_spread']:>8.2f} "
              f"{r['avg_adv_5']:>8.3f} {r['gross_edge_per_side']:>8.3f} "
              f"{r['gross_edge_per_rt']:>9.3f} {r['net_edge_per_rt']:>8.3f}")

    # ----- Aggregates -----
    section("6. Aggregate Summary")
    if all_days_results:
        n_days = len(all_days_results)
        total_snaps = sum(r["n_snapshots"] for r in all_days_results)
        total_fills = sum(r["total_fill_opps"] for r in all_days_results)
        avg_fills_per_day = total_fills / n_days

        # Weighted averages
        w_adv1 = sum(r["avg_adv_1"] * r["total_fill_opps"] for r in all_days_results)
        w_adv5 = sum(r["avg_adv_5"] * r["total_fill_opps"] for r in all_days_results)
        w_adv10 = sum(r["avg_adv_10"] * r["total_fill_opps"] for r in all_days_results)
        w_half = sum(r["half_spread"] * r["total_fill_opps"] for r in all_days_results)
        w_gross_rt = sum(r["gross_edge_per_rt"] * r["total_fill_opps"] for r in all_days_results)
        w_net_rt = sum(r["net_edge_per_rt"] * r["total_fill_opps"] for r in all_days_results)

        if total_fills > 0:
            agg_adv1 = w_adv1 / total_fills
            agg_adv5 = w_adv5 / total_fills
            agg_adv10 = w_adv10 / total_fills
            agg_half = w_half / total_fills
            agg_gross_rt = w_gross_rt / total_fills
            agg_net_rt = w_net_rt / total_fills
        else:
            agg_adv1 = agg_adv5 = agg_adv10 = agg_half = agg_gross_rt = agg_net_rt = 0

        print(f"  Days analyzed:            {n_days}")
        print(f"  Total BidAsk snapshots:   {total_snaps:,}")
        print(f"  Total fill opportunities: {total_fills:,}")
        print(f"  Avg fill opps/day:        {avg_fills_per_day:,.0f}")
        print(f"  Fill rate:                {total_fills / total_snaps * 100:.1f}%")
        print()
        print(f"  Aggregate half-spread:    {agg_half:.3f} pts")
        print(f"  Aggregate adverse@1:      {agg_adv1:.4f} pts")
        print(f"  Aggregate adverse@5:      {agg_adv5:.4f} pts")
        print(f"  Aggregate adverse@10:     {agg_adv10:.4f} pts")
        print()
        print(f"  Gross edge per RT:        {agg_gross_rt:.3f} pts  ({agg_gross_rt * POINT_VALUE_NTD:.1f} NTD)")
        print(f"  RT cost:                  {RT_COST_PTS:.1f} pts  ({RT_COST_PTS * POINT_VALUE_NTD:.0f} NTD)")
        print(f"  Net edge per RT:          {agg_net_rt:.3f} pts  ({agg_net_rt * POINT_VALUE_NTD:.1f} NTD)")
        print()

        if agg_net_rt > 0:
            print(f"  >>> VERDICT: NET POSITIVE ({agg_net_rt:.3f} pts/RT) <<<")
        else:
            print(f"  >>> VERDICT: NET NEGATIVE ({agg_net_rt:.3f} pts/RT) <<<")

        # ----- 7. Break-even Commission -----
        section("7. Break-Even Commission Analysis")
        # gross_edge_per_rt = half_spread*2 - adverse*2 (already computed)
        # net = gross_edge_per_rt - commission_both_sides - tax_both_sides
        # 0 = gross_edge_per_rt - 2*commission_pts - 2*TAX_PER_SIDE_PTS
        # commission_pts = (gross_edge_per_rt - 2*TAX_PER_SIDE_PTS) / 2
        tax_rt_pts = 2 * TAX_PER_SIDE_PTS  # 1.4 pts
        be_commission_pts = (agg_gross_rt - tax_rt_pts) / 2
        be_commission_ntd = be_commission_pts * POINT_VALUE_NTD

        print(f"  Gross edge per RT:          {agg_gross_rt:.3f} pts")
        print(f"  Tax per RT (fixed):         {tax_rt_pts:.1f} pts  ({tax_rt_pts * POINT_VALUE_NTD:.0f} NTD)")
        print(f"  Break-even commission/side: {be_commission_pts:.3f} pts  ({be_commission_ntd:.1f} NTD)")
        print(f"  Current commission/side:    {COMMISSION_PER_SIDE_PTS:.1f} pts  ({COMMISSION_PER_SIDE_PTS * POINT_VALUE_NTD:.0f} NTD)")
        print()
        if be_commission_pts > 0:
            print(f"  Need commission < {be_commission_pts:.2f} pts/side ({be_commission_ntd:.1f} NTD) to break even")
            if be_commission_pts > COMMISSION_PER_SIDE_PTS:
                print(f"  Current commission ({COMMISSION_PER_SIDE_PTS} pts) is BELOW break-even. Strategy is VIABLE.")
            else:
                print(f"  Current commission ({COMMISSION_PER_SIDE_PTS} pts) EXCEEDS break-even. Need negotiation.")
                reduction_needed = COMMISSION_PER_SIDE_PTS - be_commission_pts
                print(f"  Commission reduction needed: {reduction_needed:.2f} pts/side ({reduction_needed * POINT_VALUE_NTD:.1f} NTD)")
        else:
            print(f"  Gross edge does not even cover tax. Structurally unviable.")

        # ----- 8. Volume-Building Analysis -----
        section("8. Volume-Building Analysis")
        # Estimate contracts/day based on fill opportunities
        # Assume we can capture some fraction of fill opportunities
        capture_rates = [0.05, 0.10, 0.20, 0.50]
        print(f"  Avg fill opportunities/day: {avg_fills_per_day:,.0f}")
        print()
        print(f"  {'Capture%':>9} {'Lots/Day':>10} {'Lots/Mo':>10} {'Loss/Day(NTD)':>14} {'Loss/Mo(NTD)':>14} {'Days2_1000':>12}")
        print(f"  {'-'*9} {'-'*10} {'-'*10} {'-'*14} {'-'*14} {'-'*12}")

        for rate in capture_rates:
            lots_day = avg_fills_per_day * rate
            lots_month = lots_day * 22  # trading days
            loss_per_lot = agg_net_rt * POINT_VALUE_NTD  # NTD per RT (negative = loss)
            loss_day = lots_day * loss_per_lot
            loss_month = lots_month * loss_per_lot

            # Days to reach 1000 lots/month cumulative trading volume
            if lots_day > 0:
                # We need 1000 lots/month = ~45 lots/day
                days_to_1000 = max(1, 1000 / (lots_day * 1)) if lots_day > 0 else float('inf')
                # Actually: need to trade for enough days that monthly volume = 1000
                # If lots/day = X, need X*22 >= 1000, so X >= 45.5
                if lots_month >= 1000:
                    days_to_1000_str = f"Already@{lots_month:.0f}"
                else:
                    # Can't reach with this capture rate
                    days_to_1000_str = f"Need {1000/22:.0f}/d"
            else:
                days_to_1000_str = "N/A"

            print(f"  {rate*100:>8.0f}% {lots_day:>10,.0f} {lots_month:>10,.0f} "
                  f"{loss_day:>14,.0f} {loss_month:>14,.0f} {days_to_1000_str:>12}")

        # ----- Final Verdict -----
        section("FINAL VERDICT")
        print(f"  Symbol:                 TMFD6 (微台期)")
        print(f"  Days analyzed:          {n_days}")
        print(f"  Avg spread:             {overall_avg_spread:.2f} pts")
        print(f"  Half-spread (capture):  {agg_half:.3f} pts")
        print(f"  Adverse selection @5:   {agg_adv5:.4f} pts")
        print(f"  Gross edge/RT:          {agg_gross_rt:.3f} pts ({agg_gross_rt * POINT_VALUE_NTD:.1f} NTD)")
        print(f"  RT cost:                {RT_COST_PTS:.1f} pts ({RT_COST_PTS * POINT_VALUE_NTD:.0f} NTD)")
        print(f"  Net edge/RT:            {agg_net_rt:.3f} pts ({agg_net_rt * POINT_VALUE_NTD:.1f} NTD)")
        print(f"  Break-even comm/side:   {be_commission_pts:.3f} pts ({be_commission_ntd:.1f} NTD)")
        print()
        if agg_net_rt > 0:
            daily_profit = avg_fills_per_day * 0.10 * agg_net_rt * POINT_VALUE_NTD
            print(f"  VIABLE: Net positive at current costs.")
            print(f"  Est. daily profit @10% capture: {daily_profit:,.0f} NTD")
        else:
            print(f"  NOT VIABLE at current costs ({RT_COST_PTS} pts RT).")
            if be_commission_pts > 0:
                print(f"  Could become viable with commission <= {be_commission_pts:.2f} pts/side.")
                target_volume = 1000  # example tier
                print(f"  Volume-building path: Trade at loss to reach {target_volume} lots/month,")
                monthly_loss = avg_fills_per_day * 0.10 * 22 * abs(agg_net_rt) * POINT_VALUE_NTD
                print(f"    then negotiate. Monthly loss @10% capture: {monthly_loss:,.0f} NTD")
            else:
                print(f"  Structurally unviable. Gross edge < tax. No commission level makes this work.")


if __name__ == "__main__":
    main()
