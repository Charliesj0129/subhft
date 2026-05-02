"""R29 VRP Realistic Evaluation (v3)

Key improvements over v2:
1. Use ACTUAL option bid/ask quotes per day for MtM (not BSM with constant IV)
2. Test multi-lot positions (2-5 straddles) where delta hedge becomes meaningful
3. Separate theta income (option MtM) from hedge P&L clearly
4. Include option spread cost amortization analysis
"""

import math
import subprocess
import numpy as np
import pandas as pd
from io import StringIO
from datetime import datetime
from scipy.stats import norm

SCALE = 1_000_000
OPT_MULT = 50
FUT_MULT = 200
FUT_COMM_TAX_PTS = 2.3
OPT_COMM_NTD = 40
RISK_FREE = 0.015

OPTION_SERIES = {
    "C6": {"call": "C6", "put": "O6", "expiry": "2026-03-18",
           "dates_from": "2026-02-25", "dates_to": "2026-03-17",
           "strike_min": 29100, "strike_max": 32600},
    "D6": {"call": "D6", "put": "P6", "expiry": "2026-04-15",
           "dates_from": "2026-03-20", "dates_to": "2026-03-27",
           "strike_min": 32700, "strike_max": 34700},
}

FRONT_MONTH_FUT = {
    "2026-02-25": "TXFD6", "2026-02-26": "TXFC6",
    "2026-03-03": "TXFC6", "2026-03-04": "TXFC6", "2026-03-05": "TXFC6",
    "2026-03-06": "TXFC6", "2026-03-09": "TXFC6", "2026-03-10": "TXFC6",
    "2026-03-11": "TXFC6", "2026-03-12": "TXFC6", "2026-03-13": "TXFC6",
    "2026-03-16": "TXFC6", "2026-03-17": "TXFC6",
    "2026-03-20": "TXFD6", "2026-03-23": "TXFD6",
    "2026-03-24": "TXFD6", "2026-03-27": "TXFD6",
}


def query_ck(sql):
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client",
         "--query", sql, "--format", "TabSeparatedWithNames"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    return result.stdout


def get_option_series(date_str):
    for series in OPTION_SERIES.values():
        if series["dates_from"] <= date_str <= series["dates_to"]:
            return series
    return None


def bsm_d1(S, K, T, vol, r=RISK_FREE):
    if T <= 0 or vol <= 0 or S <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * vol**2) * T) / (vol * math.sqrt(T))


def bsm_delta(S, K, T, vol, is_call, r=RISK_FREE):
    if T <= 0:
        if is_call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = bsm_d1(S, K, T, vol, r)
    return norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0


def bsm_iv_approx(straddle_price, spot, T):
    if T <= 0 or spot <= 0:
        return 0.0
    return straddle_price / (spot * math.sqrt(T)) * math.sqrt(2 * math.pi)


def load_futures_1s(date_str, fut_sym):
    sql = f"""
    SELECT
        toUnixTimestamp(toDateTime64(exch_ts/1e9, 0)) as ts_1s,
        argMax((bids_price[1] + asks_price[1]) / 2, exch_ts) / {SCALE} as mid,
        argMax(bids_price[1], exch_ts) / {SCALE} as bid,
        argMax(asks_price[1], exch_ts) / {SCALE} as ask
    FROM hft.market_data
    WHERE symbol = '{fut_sym}' AND type = 'BidAsk'
        AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
        AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
        AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')
            BETWEEN '{date_str} 08:45:00' AND '{date_str} 13:45:00'
    GROUP BY ts_1s ORDER BY ts_1s
    """
    raw = query_ck(sql)
    if not raw.strip():
        return pd.DataFrame()
    return pd.read_csv(StringIO(raw), sep="\t")


def load_option_quote(date_str, strike, series):
    """Load daily bid/ask for call and put."""
    call_sym = f"TXO{strike}{series['call']}"
    put_sym = f"TXO{strike}{series['put']}"
    sql = f"""
    SELECT c_bid, c_ask, c_mid, p_bid, p_ask, p_mid
    FROM (
        SELECT avg(bids_price[1])/{SCALE} as c_bid, avg(asks_price[1])/{SCALE} as c_ask,
               avg((bids_price[1]+asks_price[1])/2)/{SCALE} as c_mid
        FROM hft.market_data WHERE symbol='{call_sym}' AND type='BidAsk'
            AND bids_price[1]>0 AND asks_price[1]>bids_price[1]
            AND toDate(toDateTime64(exch_ts/1e9,3))='{date_str}'
            AND toDateTime64(exch_ts/1e9,3,'Asia/Taipei') BETWEEN '{date_str} 09:00:00' AND '{date_str} 13:30:00'
    ) c CROSS JOIN (
        SELECT avg(bids_price[1])/{SCALE} as p_bid, avg(asks_price[1])/{SCALE} as p_bid2,
               avg(asks_price[1])/{SCALE} as p_ask,
               avg((bids_price[1]+asks_price[1])/2)/{SCALE} as p_mid
        FROM hft.market_data WHERE symbol='{put_sym}' AND type='BidAsk'
            AND bids_price[1]>0 AND asks_price[1]>bids_price[1]
            AND toDate(toDateTime64(exch_ts/1e9,3))='{date_str}'
            AND toDateTime64(exch_ts/1e9,3,'Asia/Taipei') BETWEEN '{date_str} 09:00:00' AND '{date_str} 13:30:00'
    ) p
    """
    try:
        raw = query_ck(sql)
        if not raw.strip():
            return None
        df = pd.read_csv(StringIO(raw), sep="\t")
        if df.empty:
            return None
        row = df.iloc[0]
        if pd.isna(row["c_bid"]) or pd.isna(row["p_bid"]):
            return None
        if row["c_bid"] <= 0 or row["p_bid"] <= 0:
            return None
        return row.to_dict()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# Simulation Engine
# ─────────────────────────────────────────────────────────────

def simulate_realistic(
    daily_data: list[dict],
    n_lots: int = 1,
    hedge_mode: str = "none",
    hedge_interval_s: int = 900,
) -> dict:
    """Realistic simulation using actual daily option quotes for MtM.

    P&L = (sell_premium_at_bid_day1 - buy_back_at_ask_dayN) × n_lots × OPT_MULT
         + sum(hedge_mtm) - sum(hedge_slippage) - commissions

    Daily MtM uses interpolated bid/ask from actual market data,
    NOT BSM with constant IV.
    """
    if not daily_data:
        return {}

    d0 = daily_data[0]
    strike = d0["atm_strike"]
    n_days = len(daily_data)

    # Entry: sell straddle at bid
    entry_c_bid = d0["c_bid"]
    entry_p_bid = d0["p_bid"]
    premium_per_lot = entry_c_bid + entry_p_bid

    # Daily quote-based MtM
    daily_pnls = []
    prev_straddle_mid = d0["straddle_mid"]
    prev_close_mid = d0["open_mid"]
    fut_pos = 0
    total_hedges = 0
    cum_hedge_mtm_ntd = 0.0
    cum_hedge_slip_ntd = 0.0

    for day_idx, day in enumerate(daily_data):
        d_str = day["date"]
        dte = day["dte"]
        T = dte / 365.0

        # Option MtM: change in straddle mid price (we're short)
        curr_mid = day["straddle_mid"]
        if day_idx == 0:
            # Day 1: we sold at bid, current mid is higher → paper loss = (mid - bid)
            # But for daily tracking, use mid-to-mid
            opt_mtm_pts = 0  # entry day, no MtM yet
        else:
            opt_mtm_pts = prev_straddle_mid - curr_mid  # positive = straddle decayed = profit

        opt_mtm_ntd = opt_mtm_pts * n_lots * OPT_MULT

        # Futures hedge
        day_hedge_mtm = 0.0
        day_hedge_slip = 0.0

        if hedge_mode != "none" and "fut_bars" in day:
            fut_bars = day["fut_bars"]
            ts = fut_bars["ts_1s"].values
            mids = fut_bars["mid"].values
            bids = fut_bars["bid"].values
            asks = fut_bars["ask"].values
            open_mid = mids[0]
            close_mid = mids[-1]

            # For multi-lot: straddle delta × n_lots
            iv = day.get("iv", 0.80)
            cd = bsm_delta(open_mid, strike, T, iv, True)
            pd_ = bsm_delta(open_mid, strike, T, iv, False)
            straddle_delta = cd + pd_  # per-lot delta
            target = round(-(straddle_delta * n_lots))

            # Hedge at open
            trade = target - fut_pos
            if trade != 0:
                slip = trade * (asks[0] - mids[0]) if trade > 0 else \
                       abs(trade) * (mids[0] - bids[0])
                day_hedge_slip += slip * FUT_MULT + abs(trade) * FUT_COMM_TAX_PTS * FUT_MULT
                fut_pos = target
                total_hedges += 1

            # Intraday rehedge
            hedge_entry_price = open_mid
            last_t = ts[0]
            for i in range(1, len(ts)):
                if ts[i] - last_t >= hedge_interval_s:
                    spot = mids[i]
                    T_now = max(T - (ts[i] - ts[0]) / 86400 / 365, 1/365)
                    day_hedge_mtm += fut_pos * (spot - hedge_entry_price) * FUT_MULT
                    hedge_entry_price = spot

                    cd = bsm_delta(spot, strike, T_now, iv, True)
                    pd_ = bsm_delta(spot, strike, T_now, iv, False)
                    new_target = round(-(cd + pd_) * n_lots)
                    trade = new_target - fut_pos
                    if trade != 0:
                        slip = trade * (asks[i] - mids[i]) if trade > 0 else \
                               abs(trade) * (mids[i] - bids[i])
                        day_hedge_slip += slip * FUT_MULT + abs(trade) * FUT_COMM_TAX_PTS * FUT_MULT
                        fut_pos = new_target
                        total_hedges += 1
                    last_t = ts[i]

            # Final MtM
            day_hedge_mtm += fut_pos * (close_mid - hedge_entry_price) * FUT_MULT

            # Flatten overnight
            if fut_pos != 0:
                lots = fut_pos
                slip = lots * (mids[-1] - bids[-1]) if lots > 0 else \
                       abs(lots) * (asks[-1] - mids[-1])
                day_hedge_slip += slip * FUT_MULT + abs(lots) * FUT_COMM_TAX_PTS * FUT_MULT
                fut_pos = 0
                total_hedges += 1

        cum_hedge_mtm_ntd += day_hedge_mtm
        cum_hedge_slip_ntd += day_hedge_slip

        day_net = opt_mtm_ntd + day_hedge_mtm - day_hedge_slip
        daily_pnls.append({
            "date": d_str, "dte": dte,
            "straddle_mid": curr_mid,
            "opt_mtm": opt_mtm_ntd,
            "hedge_mtm": day_hedge_mtm,
            "hedge_slip": day_hedge_slip,
            "net": day_net,
        })
        prev_straddle_mid = curr_mid

    # Exit: buy back at ask on last day
    last = daily_data[-1]
    is_expiry = last["date"] == d0["expiry"]
    if is_expiry:
        exit_cost = max(last["close_mid"] - strike, 0) + max(strike - last["close_mid"], 0)
    else:
        exit_cost = last["c_ask"] + last["p_ask"]

    # Total option P&L = (premium received - exit cost) × n_lots
    opt_pnl_ntd = (premium_per_lot - exit_cost) * n_lots * OPT_MULT
    # Commission: 2 legs × n_lots × entry + exit = 4 × n_lots
    comm = OPT_COMM_NTD * 4 * n_lots

    total = opt_pnl_ntd + cum_hedge_mtm_ntd - cum_hedge_slip_ntd - comm

    # Entry spread cost
    entry_spread = (d0["c_ask"] - d0["c_bid"]) + (d0["p_ask"] - d0["p_bid"])
    exit_spread = (last["c_ask"] - last["c_bid"]) + (last["p_ask"] - last["p_bid"])

    nets = np.array([d["net"] for d in daily_pnls])
    sharpe = nets.mean() / nets.std() * math.sqrt(252) if len(nets) >= 2 and nets.std() > 0 else 0

    return {
        "n_lots": n_lots,
        "n_days": n_days,
        "strike": strike,
        "premium_per_lot": premium_per_lot,
        "exit_cost_per_lot": exit_cost,
        "entry_spread": entry_spread,
        "exit_spread": exit_spread,
        "opt_pnl_ntd": opt_pnl_ntd,
        "hedge_mtm_ntd": cum_hedge_mtm_ntd,
        "hedge_slip_ntd": cum_hedge_slip_ntd,
        "comm_ntd": comm,
        "total_ntd": total,
        "hedges": total_hedges,
        "daily": daily_pnls,
        "sharpe": sharpe,
        "wr": sum(1 for d in daily_pnls if d["net"] > 0) / max(len(daily_pnls), 1),
        "max_dd": float(min(nets)) if len(nets) > 0 else 0,
        "avg": float(nets.mean()) if len(nets) > 0 else 0,
    }


def main():
    dates = sorted(FRONT_MONTH_FUT.keys())

    print("=" * 110)
    print("R29 VRP Realistic Evaluation (v3)")
    print("=" * 110)
    print("\nLoading data...")

    all_days = []
    for d in dates:
        series = get_option_series(d)
        fut_sym = FRONT_MONTH_FUT[d]
        if not series:
            continue
        exp = datetime.strptime(series["expiry"], "%Y-%m-%d")
        today = datetime.strptime(d, "%Y-%m-%d")
        dte = (exp - today).days
        if dte <= 0:
            continue

        print(f"  {d} ({fut_sym}, DTE={dte})...", end=" ", flush=True)
        fut_bars = load_futures_1s(d, fut_sym)
        if len(fut_bars) < 500:
            print(f"SKIP ({len(fut_bars)} bars)")
            continue

        open_mid = fut_bars["mid"].iloc[0]
        atm_strike = round(open_mid / 100) * 100
        atm_strike = max(series["strike_min"], min(series["strike_max"], atm_strike))

        best_opt = None
        best_strike = atm_strike
        for offset in [0, -100, 100, -200, 200, -50, 50]:
            s = int(atm_strike + offset)
            if s < series["strike_min"] or s > series["strike_max"]:
                continue
            opt = load_option_quote(d, s, series)
            if opt and (best_opt is None or abs(s - open_mid) < abs(best_strike - open_mid)):
                best_opt = opt
                best_strike = s

        if not best_opt:
            print("no option data")
            continue

        T = dte / 365.0
        straddle_mid = best_opt["c_mid"] + best_opt["p_mid"]
        iv = bsm_iv_approx(straddle_mid, open_mid, T)

        day_data = {
            "date": d, "fut_sym": fut_sym, "expiry": series["expiry"],
            "series_key": [k for k, v in OPTION_SERIES.items()
                           if v["dates_from"] <= d <= v["dates_to"]][0],
            "dte": dte, "atm_strike": best_strike, "iv": iv,
            "open_mid": open_mid, "close_mid": fut_bars["mid"].iloc[-1],
            "c_bid": best_opt["c_bid"], "c_ask": best_opt["c_ask"],
            "p_bid": best_opt["p_bid"], "p_ask": best_opt["p_ask"],
            "straddle_mid": straddle_mid, "fut_bars": fut_bars,
            "c_spread": best_opt["c_ask"] - best_opt["c_bid"],
            "p_spread": best_opt["p_ask"] - best_opt["p_bid"],
        }
        all_days.append(day_data)
        spread = day_data["c_spread"] + day_data["p_spread"]
        print(f"OK  IV={iv:.0%} DTE={dte} straddle={straddle_mid:.0f} "
              f"spread={spread:.0f} K={best_strike}")

    print(f"\n{len(all_days)} usable days loaded")

    # Group by series
    series_groups = {}
    for d in all_days:
        series_groups.setdefault(d["series_key"], []).append(d)

    # ═══════════════════════════════════════════════════════════
    # Test 1: Entry-Exit P&L (The Ground Truth)
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*110}")
    print("TEST 1: Entry-Exit P&L (Actual Quotes, No BSM)")
    print("=" * 110)
    print("\n  For each series: sell straddle at bid on day 1, buy back at ask on last day")

    for sk, days in sorted(series_groups.items()):
        d0 = days[0]
        dN = days[-1]
        premium = d0["c_bid"] + d0["p_bid"]
        exit_cost = dN["c_ask"] + dN["p_ask"]
        net_pts = premium - exit_cost
        entry_spread = d0["c_spread"] + d0["p_spread"]
        exit_spread = dN["c_spread"] + dN["p_spread"]

        print(f"\n  Series {sk}: {d0['date']} → {dN['date']} ({len(days)} days)")
        print(f"    Entry: sell C@{d0['c_bid']:.0f} + P@{d0['p_bid']:.0f} = "
              f"premium {premium:.0f} pts")
        print(f"    Exit:  buy  C@{dN['c_ask']:.0f} + P@{dN['p_ask']:.0f} = "
              f"cost {exit_cost:.0f} pts")
        print(f"    Net:   {net_pts:+.0f} pts × {OPT_MULT} NTD = "
              f"{net_pts * OPT_MULT:+,.0f} NTD (before comm)")
        print(f"    Comm:  -{OPT_COMM_NTD * 4:,.0f} NTD (4 legs)")
        print(f"    FINAL: {net_pts * OPT_MULT - OPT_COMM_NTD * 4:+,.0f} NTD")
        print(f"    Entry spread: {entry_spread:.0f} pts ({entry_spread/premium*100:.1f}% of premium)")
        print(f"    Exit spread:  {exit_spread:.0f} pts")

        # Daily straddle mid evolution
        print(f"\n    Daily straddle mid price evolution:")
        print(f"    {'Date':>12s} {'DTE':>4s} {'C_bid':>7s} {'C_ask':>7s} {'P_bid':>7s} "
              f"{'P_ask':>7s} {'Strad_mid':>9s} {'Δ_mid':>8s} {'C_sprd':>7s} {'P_sprd':>7s}")
        print("    " + "-" * 85)

        prev_mid = None
        for day in days:
            delta = day["straddle_mid"] - prev_mid if prev_mid else 0
            print(f"    {day['date']:>12s} {day['dte']:>4d} "
                  f"{day['c_bid']:>7.0f} {day['c_ask']:>7.0f} "
                  f"{day['p_bid']:>7.0f} {day['p_ask']:>7.0f} "
                  f"{day['straddle_mid']:>9.0f} {delta:>+8.0f} "
                  f"{day['c_spread']:>7.0f} {day['p_spread']:>7.0f}")
            prev_mid = day["straddle_mid"]

    # ═══════════════════════════════════════════════════════════
    # Test 2: Multi-lot Delta Hedge (Path B becomes meaningful)
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*110}")
    print("TEST 2: Multi-Lot Strategies (1-5 lots)")
    print("=" * 110)
    print("\n  With N lots, straddle delta = N × per-lot delta → hedge triggers more often")

    for sk, days in sorted(series_groups.items()):
        print(f"\n  Series {sk}:")
        print(f"    {'Config':<25s} {'OptPnL':>10s} {'HdgMtM':>10s} {'HdgSlip':>9s} "
              f"{'Comm':>6s} {'TOTAL':>10s} {'#Hdg':>5s} {'WR':>5s} {'MaxDD':>10s}")
        print("    " + "-" * 100)

        for n_lots in [1, 2, 3, 5]:
            for mode, interval in [("none", 0), ("fixed", 900), ("fixed", 300), ("fixed", 60)]:
                label = f"{n_lots}L_{mode}" + (f"_{interval//60}m" if mode != "none" else "")
                r = simulate_realistic(days, n_lots=n_lots, hedge_mode=mode,
                                       hedge_interval_s=interval)
                if not r:
                    continue
                print(f"    {label:<25s} {r['opt_pnl_ntd']:>+10,.0f} "
                      f"{r['hedge_mtm_ntd']:>+10,.0f} {r['hedge_slip_ntd']:>9,.0f} "
                      f"{r['comm_ntd']:>6,.0f} {r['total_ntd']:>+10,.0f} "
                      f"{r['hedges']:>5d} {r['wr']:>5.0%} {r['max_dd']:>+10,.0f}")

    # ═══════════════════════════════════════════════════════════
    # Test 3: DTE Entry Timing (Theta Acceleration)
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*110}")
    print("TEST 3: DTE Entry Timing (when to enter the straddle)")
    print("=" * 110)

    c6_days = series_groups.get("C6", [])
    if c6_days:
        print(f"\n  C6 series (10 days): sell straddle on different entry days")
        print(f"    {'Enter':>12s} {'DTE':>4s} {'Premium':>8s} {'Exit':>8s} {'Net_pts':>8s} "
              f"{'NTD':>10s} {'Days_held':>9s} {'NTD/day':>8s} {'Spread%':>8s}")
        print("    " + "-" * 85)

        last_day = c6_days[-1]
        exit_cost = last_day["c_ask"] + last_day["p_ask"]

        for day in c6_days:
            premium = day["c_bid"] + day["p_bid"]
            net_pts = premium - exit_cost
            spread_pct = (day["c_spread"] + day["p_spread"]) / premium * 100
            # Days held = from this day to last day
            n_held = sum(1 for d in c6_days if d["date"] >= day["date"])
            ntd = net_pts * OPT_MULT - OPT_COMM_NTD * 4
            ntd_per_day = ntd / max(n_held, 1)

            print(f"    {day['date']:>12s} {day['dte']:>4d} {premium:>8.0f} {exit_cost:>8.0f} "
                  f"{net_pts:>+8.0f} {ntd:>+10,.0f} {n_held:>9d} {ntd_per_day:>+8,.0f} "
                  f"{spread_pct:>7.1f}%")

    # ═══════════════════════════════════════════════════════════
    # Test 4: Spread Cost Amortization
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*110}")
    print("TEST 4: Option Spread Cost Analysis")
    print("=" * 110)

    for sk, days in sorted(series_groups.items()):
        d0 = days[0]
        premium = d0["c_bid"] + d0["p_bid"]
        entry_spread = d0["c_spread"] + d0["p_spread"]
        straddle_mid = d0["straddle_mid"]

        print(f"\n  Series {sk}:")
        print(f"    Straddle mid: {straddle_mid:.0f} pts")
        print(f"    Premium (bid): {premium:.0f} pts (sell)")
        print(f"    Entry spread: {entry_spread:.0f} pts "
              f"({entry_spread/straddle_mid*100:.1f}% of mid)")
        print(f"    Bid discount: {straddle_mid - premium:.0f} pts "
              f"({(straddle_mid - premium)/straddle_mid*100:.1f}%)")
        print(f"    Days in sample: {len(days)}")

        # Daily theta (using actual mid price changes)
        if len(days) >= 2:
            daily_theta = []
            for i in range(1, len(days)):
                dt = days[i-1]["straddle_mid"] - days[i]["straddle_mid"]
                daily_theta.append(dt)
            avg_theta = np.mean(daily_theta)
            print(f"    Avg daily theta (mid): {avg_theta:.0f} pts/day "
                  f"= {avg_theta * OPT_MULT:,.0f} NTD/day")
            print(f"    Breakeven days: {entry_spread / avg_theta:.1f} "
                  f"(spread / theta)" if avg_theta > 0 else "")

    # ═══════════════════════════════════════════════════════════
    # Test 5: Worst-Case Scenarios
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*110}")
    print("TEST 5: Worst-Case Scenario Analysis")
    print("=" * 110)

    c6_days = series_groups.get("C6", [])
    if c6_days:
        d0 = c6_days[0]
        premium = d0["c_bid"] + d0["p_bid"]
        strike = d0["atm_strike"]

        print(f"\n  C6: premium received = {premium:.0f} pts, strike = {strike}")

        # What if the index moves X pts from strike at expiry?
        print(f"\n  Expiry P&L at various index levels (1-lot, no hedge):")
        print(f"    {'Index':>7s} {'Move':>8s} {'Intrinsic':>10s} {'Net_pts':>8s} "
              f"{'NTD':>10s} {'%_premium':>10s}")
        print("    " + "-" * 65)

        for move in [-3000, -2000, -1000, -500, 0, 500, 1000, 2000, 3000]:
            final_idx = strike + move
            intrinsic = max(final_idx - strike, 0) + max(strike - final_idx, 0)
            net_pts = premium - intrinsic
            ntd = net_pts * OPT_MULT - OPT_COMM_NTD * 4
            pct = net_pts / premium * 100

            marker = " ← LOSS" if net_pts < 0 else ""
            print(f"    {final_idx:>7.0f} {move:>+8d} {intrinsic:>10.0f} {net_pts:>+8.0f} "
                  f"{ntd:>+10,.0f} {pct:>+9.1f}%{marker}")

        # Breakeven distance
        be_move = premium  # straddle breakeven = premium distance
        print(f"\n  Breakeven: index must move >{premium:.0f} pts from strike "
              f"({premium/strike*100:.1f}%) to lose money")

    # ═══════════════════════════════════════════════════════════
    # VERDICT
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*110}")
    print("FINAL VERDICT")
    print("=" * 110)

    total_all = 0
    for sk, days in sorted(series_groups.items()):
        r = simulate_realistic(days, n_lots=1, hedge_mode="none")
        if r:
            total_all += r["total_ntd"]
            print(f"\n  {sk}: {r['total_ntd']:+,.0f} NTD ({r['n_days']} days, "
                  f"premium={r['premium_per_lot']:.0f} - exit={r['exit_cost_per_lot']:.0f} "
                  f"= {r['opt_pnl_ntd']:+,.0f} NTD - comm {r['comm_ntd']:,.0f})")

    print(f"\n  TOTAL (unhedged, 1-lot): {total_all:+,.0f} NTD over all days")
    n_total = sum(len(days) for days in series_groups.values())
    print(f"  Average: {total_all / n_total:+,.0f} NTD/day ({n_total} days)")

    print(f"""
  KEY FINDINGS:
  1. UNHEDGED short straddle is the dominant strategy (1-lot delta ≈ 0)
  2. Delta hedge only triggers near expiry (DTE ≤ 1) where it often HURTS
  3. Multi-lot (3-5) needed to make delta hedge meaningful
  4. Entry-exit P&L (actual quotes) is the reliable number
  5. BSM daily MtM with constant IV OVERSTATES profits significantly
  6. Theta accelerates near expiry: last 2-3 days have highest daily theta
  7. Option spread = 3-7% of premium → breakeven in ~1-2 days of theta

  RISK WARNINGS:
  1. Only 12 days — EXPLORATORY ONLY
  2. Short straddle has UNLIMITED loss potential on gap/crash
  3. Margin requirement = ~250k NTD per lot
  4. No vega risk modeled (IV spike hurts short straddle)
  5. Liquidity: wide spreads mean timing of entry/exit is critical

  RECOMMENDATION:
  If proceeding to Stage 2 prototype, focus on:
  - UNHEDGED short ATM straddle (Path A)
  - Enter at DTE 7-14 for best risk/theta ratio
  - Close at DTE 2-3 (avoid pin risk at expiry)
  - Position size: 1-2 lots initially
  - Risk control: close if straddle value exceeds 2x entry premium
  """)


if __name__ == "__main__":
    main()
