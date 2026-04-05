"""R29 VRP Dual-Path Evaluation

Path A: Pure VRP — no/minimal delta hedge, collect theta, accept gamma risk
Path B: Gamma Scalping — optimize delta hedge timing for directional profit

Both paths use the same entry (short ATM straddle) but differ in hedging approach.
Also tests DTE entry timing: is it better to enter early or late in the cycle?
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


# ─────────────────────────────────────────────────────────────
# BSM
# ─────────────────────────────────────────────────────────────

def bsm_d1(S, K, T, vol, r=RISK_FREE):
    if T <= 0 or vol <= 0 or S <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * vol**2) * T) / (vol * math.sqrt(T))

def bsm_price(S, K, T, vol, is_call, r=RISK_FREE):
    if T <= 0:
        return max(S - K, 0) if is_call else max(K - S, 0)
    d1 = bsm_d1(S, K, T, vol, r)
    d2 = d1 - vol * math.sqrt(T)
    if is_call:
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def bsm_delta(S, K, T, vol, is_call, r=RISK_FREE):
    if T <= 0:
        if is_call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = bsm_d1(S, K, T, vol, r)
    return norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0

def bsm_gamma(S, K, T, vol, r=RISK_FREE):
    if T <= 0 or vol <= 0 or S <= 0:
        return 0.0
    d1 = bsm_d1(S, K, T, vol, r)
    return norm.pdf(d1) / (S * vol * math.sqrt(T))

def bsm_iv_approx(straddle_price, spot, T):
    if T <= 0 or spot <= 0:
        return 0.0
    return straddle_price / (spot * math.sqrt(T)) * math.sqrt(2 * math.pi)


# ─────────────────────────────────────────────────────────────
# Data Loading (reuse from v2)
# ─────────────────────────────────────────────────────────────

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
        SELECT avg(bids_price[1])/{SCALE} as p_bid, avg(asks_price[1])/{SCALE} as p_ask,
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
# Unified Simulation Engine
# ─────────────────────────────────────────────────────────────

def simulate_straddle(
    daily_data: list[dict],
    hedge_config: dict,
) -> dict:
    """Unified simulation for both Path A and Path B.

    hedge_config options:
        mode: "none" | "open_only" | "fixed" | "smart" | "burst"
        interval_s: seconds between hedges (for fixed/smart)
        vol_thresh: vol ratio threshold for smart hedge
        burst_thresh: pts/min threshold for burst emergency hedge
        delta_thresh: delta threshold before hedge triggers (for filtered modes)
        flatten_overnight: bool
    """
    if not daily_data:
        return {}

    mode = hedge_config.get("mode", "none")
    interval_s = hedge_config.get("interval_s", 300)
    vol_thresh = hedge_config.get("vol_thresh", 1.5)
    burst_thresh = hedge_config.get("burst_thresh", 100.0)
    delta_thresh = hedge_config.get("delta_thresh", 0.0)
    flatten_overnight = hedge_config.get("flatten_overnight", True)

    strike = daily_data[0]["atm_strike"]
    iv = daily_data[0]["iv"]

    # Entry
    d0 = daily_data[0]
    premium_received = d0["c_bid"] + d0["p_bid"]
    entry_spot = d0["open_mid"]

    daily_pnls = []
    prev_close_mid = entry_spot
    fut_pos = 0
    total_hedges = 0
    cum_hedge_slippage_ntd = 0.0
    cum_hedge_mtm_ntd = 0.0

    for day_idx, day in enumerate(daily_data):
        d_str = day["date"]
        fut_bars = day["fut_bars"]
        dte = day["dte"]
        T = dte / 365.0
        T_end = max((dte - 1) / 365.0, 0)

        ts = fut_bars["ts_1s"].values
        mids = fut_bars["mid"].values
        bids = fut_bars["bid"].values
        asks = fut_bars["ask"].values
        n = len(ts)

        open_mid = mids[0]
        close_mid = mids[-1]

        day_hedge_slip_ntd = 0.0
        day_hedge_mtm_ntd = 0.0

        # Option mark-to-market
        opt_val_open = bsm_price(open_mid, strike, T, iv, True) + \
                       bsm_price(open_mid, strike, T, iv, False)
        opt_val_close = bsm_price(close_mid, strike, T_end, iv, True) + \
                        bsm_price(close_mid, strike, T_end, iv, False)
        opt_mtm_ntd = (opt_val_open - opt_val_close) * OPT_MULT

        # Decompose: theta and gamma
        opt_val_close_same = bsm_price(open_mid, strike, T_end, iv, True) + \
                             bsm_price(open_mid, strike, T_end, iv, False)
        pure_theta_ntd = (opt_val_open - opt_val_close_same) * OPT_MULT
        gamma_ntd = opt_mtm_ntd - pure_theta_ntd

        # Overnight gap MtM
        overnight_ntd = 0.0
        if day_idx > 0:
            prev_T_end = T
            val_prev = bsm_price(prev_close_mid, strike, prev_T_end, iv, True) + \
                       bsm_price(prev_close_mid, strike, prev_T_end, iv, False)
            val_now = bsm_price(open_mid, strike, T, iv, True) + \
                      bsm_price(open_mid, strike, T, iv, False)
            overnight_ntd = (val_prev - val_now) * OPT_MULT
            # Carry overnight hedge MtM
            if not flatten_overnight and fut_pos != 0:
                day_hedge_mtm_ntd += fut_pos * (open_mid - prev_close_mid) * FUT_MULT

        # ── Hedge Logic ──
        hedge_entry_price = open_mid

        def do_hedge(idx, pos):
            nonlocal day_hedge_slip_ntd, day_hedge_mtm_ntd, total_hedges, hedge_entry_price
            spot = mids[idx]
            # MtM on current position
            day_hedge_mtm_ntd += pos * (spot - hedge_entry_price) * FUT_MULT
            hedge_entry_price = spot

            cd = bsm_delta(spot, strike, max(T - (ts[idx] - ts[0]) / 86400 / 365, 1/365), iv, True)
            pd_ = bsm_delta(spot, strike, max(T - (ts[idx] - ts[0]) / 86400 / 365, 1/365), iv, False)
            new_target = round(-(cd + pd_))
            trade = new_target - pos

            if trade != 0:
                slip = trade * (asks[idx] - mids[idx]) if trade > 0 else \
                       abs(trade) * (mids[idx] - bids[idx])
                day_hedge_slip_ntd += slip * FUT_MULT + abs(trade) * FUT_COMM_TAX_PTS * FUT_MULT
                total_hedges += 1
                return new_target
            return pos

        if mode == "none":
            # Path A: No hedge at all
            pass

        elif mode == "open_only":
            # Path A variant: hedge only at open, no rebalance
            fut_pos = do_hedge(0, fut_pos)

        elif mode == "open_close":
            # Path A variant: hedge at open and close only
            fut_pos = do_hedge(0, fut_pos)
            # Close hedge at end (separate from flatten)
            if n > 100:
                mid_idx = n // 2
                fut_pos = do_hedge(mid_idx, fut_pos)

        elif mode == "fixed":
            # Path B: fixed interval hedge
            fut_pos = do_hedge(0, fut_pos)
            last_t = ts[0]
            for i in range(1, n):
                if ts[i] - last_t >= interval_s:
                    fut_pos = do_hedge(i, fut_pos)
                    last_t = ts[i]

        elif mode == "smart":
            # Path B: vol-adaptive hedge
            fut_pos = do_hedge(0, fut_pos)
            last_t = ts[0]
            for i in range(1, n):
                elapsed = ts[i] - last_t
                lookback = min(i, 60)
                if lookback < 10:
                    if elapsed >= interval_s:
                        fut_pos = do_hedge(i, fut_pos)
                        last_t = ts[i]
                    continue

                recent_rets = np.diff(mids[i-lookback:i+1]) / mids[i-lookback:i]
                recent_vol = float(np.std(recent_rets)) if len(recent_rets) > 1 else 0
                all_rets = np.diff(mids[:i+1]) / mids[:i]
                session_vol = float(np.std(all_rets)) if len(all_rets) > 10 else recent_vol
                vol_ratio = recent_vol / session_vol if session_vol > 0 else 1.0

                if vol_ratio > vol_thresh and elapsed >= interval_s:
                    fut_pos = do_hedge(i, fut_pos)
                    last_t = ts[i]
                elif elapsed >= interval_s * 3:
                    fut_pos = do_hedge(i, fut_pos)
                    last_t = ts[i]

        elif mode == "burst":
            # Path B: burst-triggered hedge (BurstDetector-like)
            # Normal: hedge at long intervals
            # Burst: immediate hedge when price moves fast
            fut_pos = do_hedge(0, fut_pos)
            last_t = ts[0]
            for i in range(1, n):
                elapsed = ts[i] - last_t

                # Burst detection: price move in last 30s
                lookback_30s = max(1, min(i, 30))
                price_move_30s = abs(mids[i] - mids[max(0, i - lookback_30s)])
                move_per_min = price_move_30s * 2  # extrapolate to per-min

                if move_per_min > burst_thresh:
                    # Emergency hedge
                    fut_pos = do_hedge(i, fut_pos)
                    last_t = ts[i]
                elif elapsed >= interval_s:
                    # Normal periodic hedge
                    fut_pos = do_hedge(i, fut_pos)
                    last_t = ts[i]

        elif mode == "delta_band":
            # Path B: hedge only when delta exceeds threshold
            fut_pos = do_hedge(0, fut_pos)
            last_t = ts[0]
            for i in range(1, n):
                T_now = max(T - (ts[i] - ts[0]) / 86400 / 365, 1/365)
                cd = bsm_delta(mids[i], strike, T_now, iv, True)
                pd_ = bsm_delta(mids[i], strike, T_now, iv, False)
                current_delta = -(cd + pd_)
                # Only hedge if delta exceeds band
                if abs(current_delta - fut_pos) > delta_thresh:
                    fut_pos = do_hedge(i, fut_pos)
                    last_t = ts[i]
                elif ts[i] - last_t >= 3600:  # max 1hr between hedges
                    fut_pos = do_hedge(i, fut_pos)
                    last_t = ts[i]

        # Final MtM
        day_hedge_mtm_ntd += fut_pos * (close_mid - hedge_entry_price) * FUT_MULT

        # Flatten overnight
        if flatten_overnight and fut_pos != 0:
            lots = fut_pos
            slip = lots * (mids[-1] - bids[-1]) if lots > 0 else \
                   abs(lots) * (asks[-1] - mids[-1])
            day_hedge_slip_ntd += slip * FUT_MULT + abs(lots) * FUT_COMM_TAX_PTS * FUT_MULT
            fut_pos = 0
            total_hedges += 1

        cum_hedge_slippage_ntd += day_hedge_slip_ntd
        cum_hedge_mtm_ntd += day_hedge_mtm_ntd

        day_net = opt_mtm_ntd + overnight_ntd + day_hedge_mtm_ntd - day_hedge_slip_ntd
        daily_pnls.append({
            "date": d_str, "dte": dte,
            "opt_mtm": opt_mtm_ntd, "theta": pure_theta_ntd, "gamma": gamma_ntd,
            "overnight": overnight_ntd, "hedge_mtm": day_hedge_mtm_ntd,
            "hedge_slip": day_hedge_slip_ntd, "net": day_net,
            "open": open_mid, "close": close_mid,
            "gap": open_mid - prev_close_mid if day_idx > 0 else 0,
        })
        prev_close_mid = close_mid

    # Exit cost
    last_day = daily_data[-1]
    is_expiry = last_day["date"] == daily_data[0]["expiry"]
    if is_expiry:
        exit_cost = max(last_day["close_mid"] - strike, 0) + max(strike - last_day["close_mid"], 0)
    else:
        exit_cost = last_day.get("c_ask", 0) + last_day.get("p_ask", 0)

    opt_net_ntd = (premium_received - exit_cost) * OPT_MULT
    comm = OPT_COMM_NTD * 4
    total = opt_net_ntd + cum_hedge_mtm_ntd - cum_hedge_slippage_ntd - comm

    nets = np.array([d["net"] for d in daily_pnls])
    sharpe = nets.mean() / nets.std() * math.sqrt(252) if len(nets) >= 2 and nets.std() > 0 else 0

    return {
        "n_days": len(daily_data),
        "strike": strike,
        "premium_pts": premium_received,
        "exit_cost_pts": exit_cost,
        "opt_net_ntd": opt_net_ntd,
        "hedge_mtm_ntd": cum_hedge_mtm_ntd,
        "hedge_slip_ntd": cum_hedge_slippage_ntd,
        "comm_ntd": comm,
        "total_ntd": total,
        "hedges": total_hedges,
        "daily": daily_pnls,
        "sharpe": sharpe,
        "wr": sum(1 for d in daily_pnls if d["net"] > 0) / len(daily_pnls),
        "max_dd": float(min(nets)),
        "avg": float(nets.mean()),
        "std": float(nets.std()),
    }


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    dates = sorted(FRONT_MONTH_FUT.keys())

    print("=" * 100)
    print("R29 VRP Dual-Path Evaluation")
    print("=" * 100)
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

        mids_arr = fut_bars["mid"].values
        rets = np.diff(mids_arr) / mids_arr[:-1]
        rv = float(np.std(rets) * np.sqrt(252 * len(rets)))

        day_data = {
            "date": d, "fut_sym": fut_sym, "expiry": series["expiry"],
            "series_key": [k for k, v in OPTION_SERIES.items()
                           if v["dates_from"] <= d <= v["dates_to"]][0],
            "dte": dte, "atm_strike": best_strike, "iv": iv, "rv": rv,
            "open_mid": open_mid, "close_mid": fut_bars["mid"].iloc[-1],
            "c_bid": best_opt["c_bid"], "c_ask": best_opt["c_ask"],
            "p_bid": best_opt["p_bid"], "p_ask": best_opt["p_ask"],
            "straddle_mid": straddle_mid, "fut_bars": fut_bars,
        }
        all_days.append(day_data)
        print(f"OK  IV={iv:.0%} RV={rv:.0%} DTE={dte} K={best_strike}")

    print(f"\n{len(all_days)} usable days loaded")

    # Group by series
    series_groups = {}
    for d in all_days:
        series_groups.setdefault(d["series_key"], []).append(d)

    # ═══════════════════════════════════════════════════════════
    # Define all strategies
    # ═══════════════════════════════════════════════════════════

    strategies = {
        # ── Path A: Pure VRP ──
        "A1_no_hedge":    {"mode": "none"},
        "A2_open_only":   {"mode": "open_only"},
        "A3_open_close":  {"mode": "open_close"},

        # ── Path B: Gamma Scalping ──
        "B1_fixed_1min":  {"mode": "fixed", "interval_s": 60},
        "B2_fixed_5min":  {"mode": "fixed", "interval_s": 300},
        "B3_fixed_15min": {"mode": "fixed", "interval_s": 900},
        "B4_fixed_30min": {"mode": "fixed", "interval_s": 1800},
        "B5_smart_5min":  {"mode": "smart", "interval_s": 300, "vol_thresh": 1.5},
        "B6_smart_15min": {"mode": "smart", "interval_s": 900, "vol_thresh": 1.5},
        "B7_burst_15min": {"mode": "burst", "interval_s": 900, "burst_thresh": 80},
        "B8_burst_30min": {"mode": "burst", "interval_s": 1800, "burst_thresh": 100},
        "B9_delta_band":  {"mode": "delta_band", "delta_thresh": 0.3},
        "B10_delta_wide": {"mode": "delta_band", "delta_thresh": 0.5},
    }

    # ═══════════════════════════════════════════════════════════
    # Run all strategies per series
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*100}")
    print("Per-Series Results")
    print("=" * 100)

    all_results = {}

    for sk, days in sorted(series_groups.items()):
        print(f"\n  Series {sk}: {len(days)} days, expiry={days[0]['expiry']}")
        print(f"    {'Strategy':<20s} {'Total':>10s} {'Avg/Day':>10s} {'OptNet':>10s} "
              f"{'HdgMtM':>10s} {'HdgSlip':>9s} {'#Hdg':>5s} {'WR':>5s} {'Sharpe':>7s} {'MaxDD':>10s}")
        print("    " + "-" * 100)

        for name, config in strategies.items():
            result = simulate_straddle(days, config)
            if not result:
                continue

            all_results.setdefault(name, []).append(result)

            print(f"    {name:<20s} "
                  f"{result['total_ntd']:>+10,.0f} "
                  f"{result['avg']:>+10,.0f} "
                  f"{result['opt_net_ntd']:>+10,.0f} "
                  f"{result['hedge_mtm_ntd']:>+10,.0f} "
                  f"{result['hedge_slip_ntd']:>9,.0f} "
                  f"{result['hedges']:>5d} "
                  f"{result['wr']:>5.0%} "
                  f"{result['sharpe']:>7.1f} "
                  f"{result['max_dd']:>+10,.0f}")

    # ═══════════════════════════════════════════════════════════
    # Aggregate across series
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*100}")
    print("Aggregate Results (All Days Combined)")
    print("=" * 100)

    holdout_n = 5
    summary_rows = []

    print(f"\n  {'Strategy':<20s} {'Days':>4s} {'TotNTD':>10s} {'Avg/Day':>10s} "
          f"{'WR':>5s} {'Sharpe':>7s} {'MaxDD':>10s} | "
          f"{'Train':>10s} {'Holdout':>10s} {'HO_WR':>5s}")
    print("  " + "-" * 110)

    for name in strategies:
        if name not in all_results:
            continue

        # Merge daily pnls across series
        daily = []
        for r in all_results[name]:
            daily.extend(r["daily"])
        daily.sort(key=lambda x: x["date"])

        nets = np.array([d["net"] for d in daily])
        n = len(nets)
        split = max(n - holdout_n, 1)
        train = nets[:split]
        holdout = nets[split:]

        sharpe = nets.mean() / nets.std() * math.sqrt(252) if nets.std() > 0 else 0
        wr = sum(nets > 0) / n
        ho_wr = sum(holdout > 0) / len(holdout) if len(holdout) > 0 else 0

        path = "A" if name.startswith("A") else "B"
        summary_rows.append({
            "name": name, "path": path, "n": n,
            "total": nets.sum(), "avg": nets.mean(), "wr": wr,
            "sharpe": sharpe, "maxdd": nets.min(),
            "train_avg": train.mean(), "holdout_avg": holdout.mean(),
            "ho_wr": ho_wr,
        })

        print(f"  {name:<20s} {n:>4d} {nets.sum():>+10,.0f} {nets.mean():>+10,.0f} "
              f"{wr:>5.0%} {sharpe:>7.1f} {nets.min():>+10,.0f} | "
              f"{train.mean():>+10,.0f} {holdout.mean():>+10,.0f} {ho_wr:>5.0%}")

    # ═══════════════════════════════════════════════════════════
    # Path A vs Path B Summary
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*100}")
    print("PATH A (Pure VRP) vs PATH B (Gamma Scalping)")
    print("=" * 100)

    for path_label in ["A", "B"]:
        path_rows = [r for r in summary_rows if r["path"] == path_label]
        if not path_rows:
            continue

        best = max(path_rows, key=lambda x: x["sharpe"])
        worst = min(path_rows, key=lambda x: x["sharpe"])

        print(f"\n  PATH {path_label}:")
        print(f"    Best Sharpe:  {best['name']:<20s} Sharpe={best['sharpe']:.1f}  "
              f"avg={best['avg']:+,.0f}  wr={best['wr']:.0%}  holdout={best['holdout_avg']:+,.0f}")
        print(f"    Worst Sharpe: {worst['name']:<20s} Sharpe={worst['sharpe']:.1f}  "
              f"avg={worst['avg']:+,.0f}  wr={worst['wr']:.0%}  holdout={worst['holdout_avg']:+,.0f}")

    # ═══════════════════════════════════════════════════════════
    # DTE Entry Timing Analysis
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*100}")
    print("DTE Entry Timing Analysis (Smart Hedge)")
    print("=" * 100)

    # For C6 series, test entering at different DTE cutoffs
    c6_days = series_groups.get("C6", [])
    if c6_days:
        print("\n  C6 series — enter only when DTE ≤ threshold:")
        print(f"    {'DTE≤':>6s} {'Days':>4s} {'TotNTD':>10s} {'Avg/Day':>10s} "
              f"{'WR':>5s} {'Sharpe':>7s} {'MaxDD':>10s}")
        print("    " + "-" * 60)

        for dte_max in [14, 10, 7, 5, 3]:
            filtered = [d for d in c6_days if d["dte"] <= dte_max]
            if len(filtered) < 2:
                continue

            result = simulate_straddle(filtered, {"mode": "smart", "interval_s": 300, "vol_thresh": 1.5})
            if not result:
                continue
            nets = np.array([d["net"] for d in result["daily"]])
            sharpe = nets.mean() / nets.std() * math.sqrt(252) if nets.std() > 0 else 0
            print(f"    {dte_max:>6d} {len(filtered):>4d} {nets.sum():>+10,.0f} "
                  f"{nets.mean():>+10,.0f} {sum(nets > 0)/len(nets):>5.0%} "
                  f"{sharpe:>7.1f} {nets.min():>+10,.0f}")

    # ═══════════════════════════════════════════════════════════
    # Risk Analysis
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*100}")
    print("Risk Analysis (Best Path A vs Best Path B)")
    print("=" * 100)

    for name in ["A1_no_hedge", "B5_smart_5min"]:
        if name not in all_results:
            continue
        daily = []
        for r in all_results[name]:
            daily.extend(r["daily"])
        daily.sort(key=lambda x: x["date"])

        nets = np.array([d["net"] for d in daily])
        cum = np.cumsum(nets)

        print(f"\n  {name}:")
        print(f"    Total: {nets.sum():+,.0f} NTD over {len(nets)} days")
        print(f"    Max drawdown (peak to trough): "
              f"{(cum - np.maximum.accumulate(cum)).min():+,.0f} NTD")
        print(f"    Worst day: {nets.min():+,.0f} NTD")
        print(f"    Best day:  {nets.max():+,.0f} NTD")
        print(f"    Std:       {nets.std():,.0f} NTD")
        print(f"    Calmar:    {nets.mean() / abs(nets.min()) * 252:.2f}" if nets.min() < 0 else "")

        # Margin requirement estimate
        # Short straddle margin ≈ premium + max(call margin, put margin)
        # Rough estimate: 15% of underlying for options + futures initial margin
        premium = all_results[name][0]["premium_pts"]
        underlying = daily[0]["open"]
        margin_est = underlying * 0.15 * OPT_MULT  # rough option margin
        roi = nets.sum() / margin_est * 100 if margin_est > 0 else 0
        print(f"    Est. margin: ~{margin_est:,.0f} NTD")
        print(f"    ROI on margin: {roi:.1f}% ({len(nets)} days)")

    # ═══════════════════════════════════════════════════════════
    # Daily Detail: Path A (no hedge) vs Path B (smart)
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*100}")
    print("Daily Comparison: A1 (No Hedge) vs B5 (Smart 5min)")
    print("=" * 100)

    a1_daily = []
    b5_daily = []
    for r in all_results.get("A1_no_hedge", []):
        a1_daily.extend(r["daily"])
    for r in all_results.get("B5_smart_5min", []):
        b5_daily.extend(r["daily"])
    a1_daily.sort(key=lambda x: x["date"])
    b5_daily.sort(key=lambda x: x["date"])

    if a1_daily and b5_daily:
        print(f"\n  {'Date':>12s} {'DTE':>4s} {'Gap':>7s} | "
              f"{'A1_Net':>10s} {'A1_OptMtM':>10s} | "
              f"{'B5_Net':>10s} {'B5_OptMtM':>10s} {'B5_HdgMtM':>10s} {'B5_Slip':>8s}")
        print("  " + "-" * 95)

        a1_cum = 0
        b5_cum = 0
        for a, b in zip(a1_daily, b5_daily):
            a1_cum += a["net"]
            b5_cum += b["net"]
            print(f"  {a['date']:>12s} {a['dte']:>4d} {a['gap']:>+7.0f} | "
                  f"{a['net']:>+10,.0f} {a['opt_mtm']:>+10,.0f} | "
                  f"{b['net']:>+10,.0f} {b['opt_mtm']:>+10,.0f} "
                  f"{b['hedge_mtm']:>+10,.0f} {b['hedge_slip']:>8,.0f}")
        print(f"  {'CUMULATIVE':>12s} {'':>4s} {'':>7s} | "
              f"{a1_cum:>+10,.0f} {'':>10s} | "
              f"{b5_cum:>+10,.0f}")

    # ═══════════════════════════════════════════════════════════
    # VERDICT
    # ═══════════════════════════════════════════════════════════

    print(f"\n{'='*100}")
    print("VERDICT")
    print("=" * 100)

    path_a_rows = [r for r in summary_rows if r["path"] == "A"]
    path_b_rows = [r for r in summary_rows if r["path"] == "B"]

    if path_a_rows:
        best_a = max(path_a_rows, key=lambda x: x["sharpe"])
        print(f"\n  PATH A (Pure VRP) — Best: {best_a['name']}")
        print(f"    Sharpe={best_a['sharpe']:.1f}  avg={best_a['avg']:+,.0f}/day  "
              f"wr={best_a['wr']:.0%}  holdout={best_a['holdout_avg']:+,.0f}/day")
        if best_a["holdout_avg"] > 0 and best_a["sharpe"] > 1.0:
            print(f"    → PASS: Positive holdout with reasonable Sharpe")
        else:
            print(f"    → FAIL: {'Negative holdout' if best_a['holdout_avg'] <= 0 else 'Low Sharpe'}")

    if path_b_rows:
        best_b = max(path_b_rows, key=lambda x: x["sharpe"])
        print(f"\n  PATH B (Gamma Scalping) — Best: {best_b['name']}")
        print(f"    Sharpe={best_b['sharpe']:.1f}  avg={best_b['avg']:+,.0f}/day  "
              f"wr={best_b['wr']:.0%}  holdout={best_b['holdout_avg']:+,.0f}/day")
        if best_b["holdout_avg"] > 0 and best_b["sharpe"] > 1.0:
            print(f"    → PASS: Positive holdout with reasonable Sharpe")
        else:
            print(f"    → FAIL: {'Negative holdout' if best_b['holdout_avg'] <= 0 else 'Low Sharpe'}")

    print("\n  CRITICAL CAVEATS:")
    print("  1. 12 days = STATISTICALLY MEANINGLESS. This is hypothesis exploration only.")
    print("  2. Constant IV assumption ignores vol-of-vol and skew dynamics.")
    print("  3. No margin/capital constraint — real margin calls could force liquidation.")
    print("  4. Path B hedge MtM is path-dependent — same strategy, different market → different result.")
    print("  5. Option liquidity: wide spreads (30-140 pts) make entry/exit timing critical.")
    print("  6. Tail risk: short straddle can lose multiples of premium on gap/crash events.")


if __name__ == "__main__":
    main()
