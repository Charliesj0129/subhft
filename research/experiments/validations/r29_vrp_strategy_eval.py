"""R29 VRP Strategy Evaluation

Full pipeline:
1. Compute daily IV (ATM straddle) vs RV (futures) for all days
2. Simulate short straddle + delta hedge at multiple frequencies
3. Compare smart hedge (LOB-guided) vs dumb hedge (fixed interval)

Conservative execution: options at bid/ask, futures at bid/ask + comm/tax.
"""

import math
import subprocess
import numpy as np
import pandas as pd
from io import StringIO
from datetime import datetime, timedelta

SCALE = 1_000_000
OPT_MULT = 50    # NTD per option point
FUT_MULT = 200   # NTD per futures point
FUT_COMM_TAX_PTS = 2.3  # futures commission + tax per RT in pts
OPT_COMM_NTD = 40  # option commission per RT in NTD (~0.8 pts)

# TXO expiry dates and call/put suffix mapping
# Call suffix / Put suffix / expiry date / usable strike range
OPTION_SERIES = {
    # B6/N6 strikes 22400-23400: deep OTM, skip
    "C6": {"call": "C6", "put": "O6", "expiry": "2026-03-18",
           "dates_from": "2026-02-25", "dates_to": "2026-03-18",
           "strike_min": 29100, "strike_max": 32600},
    "D6": {"call": "D6", "put": "P6", "expiry": "2026-04-15",
           "dates_from": "2026-03-20", "dates_to": "2026-04-15",
           "strike_min": 32700, "strike_max": 34700},
}

# Front-month futures (only dates with usable options)
FRONT_MONTH_FUT = {
    "2026-02-25": "TXFD6", "2026-02-26": "TXFC6",
    "2026-03-03": "TXFC6", "2026-03-04": "TXFC6", "2026-03-05": "TXFC6",
    "2026-03-06": "TXFC6", "2026-03-09": "TXFC6", "2026-03-10": "TXFC6",
    "2026-03-11": "TXFC6", "2026-03-12": "TXFC6", "2026-03-13": "TXFC6",
    "2026-03-16": "TXFC6", "2026-03-17": "TXFC6",
    "2026-03-20": "TXFD6", "2026-03-23": "TXFD6",
    "2026-03-24": "TXFD6", "2026-03-27": "TXFD6",
}


def get_option_series(date_str: str) -> dict | None:
    """Get the appropriate option series for a given date."""
    for series in OPTION_SERIES.values():
        if series["dates_from"] <= date_str <= series["dates_to"]:
            return series
    return None


def query_ck(sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client",
         "--query", sql, "--format", "TabSeparatedWithNames"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    return result.stdout


def bsm_iv_approx(straddle_price: float, spot: float, T: float) -> float:
    """Brenner-Subrahmanyam approximation: IV ≈ straddle / (spot * sqrt(T)) * sqrt(2π)."""
    if T <= 0 or spot <= 0:
        return 0.0
    return straddle_price / (spot * math.sqrt(T)) * math.sqrt(2 * math.pi)


def bsm_delta(spot: float, strike: float, T: float, vol: float, is_call: bool) -> float:
    """Simple BSM delta (no dividend, r≈0)."""
    if T <= 0 or vol <= 0:
        return 1.0 if is_call else -1.0
    from scipy.stats import norm
    d1 = (math.log(spot / strike) + 0.5 * vol**2 * T) / (vol * math.sqrt(T))
    return norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0


def bsm_gamma(spot: float, strike: float, T: float, vol: float) -> float:
    """BSM gamma."""
    if T <= 0 or vol <= 0 or spot <= 0:
        return 0.0
    from scipy.stats import norm
    d1 = (math.log(spot / strike) + 0.5 * vol**2 * T) / (vol * math.sqrt(T))
    return norm.pdf(d1) / (spot * vol * math.sqrt(T))


# ─────────────────────────────────────────────────────────────────────────
# Part 1: Daily IV vs RV
# ─────────────────────────────────────────────────────────────────────────

def compute_daily_iv_rv(date_str: str) -> dict | None:
    """Compute IV from ATM straddle and RV from futures for one day."""
    fut_sym = FRONT_MONTH_FUT.get(date_str)
    if not fut_sym:
        return None

    series = get_option_series(date_str)
    if not series:
        return None

    d = datetime.strptime(date_str, "%Y-%m-%d")
    exp_d = datetime.strptime(series["expiry"], "%Y-%m-%d")
    dte = (exp_d - d).days
    if dte <= 0:
        return None
    T = dte / 365.0

    # Get futures 1s bars for the day, compute RV in Python
    sql_fut = f"""
    SELECT
        toUnixTimestamp(toDateTime64(exch_ts/1e9, 0)) as ts_1s,
        argMax((bids_price[1] + asks_price[1]) / 2, exch_ts) / {SCALE} as mid
    FROM hft.market_data
    WHERE symbol = '{fut_sym}' AND type = 'BidAsk'
        AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
        AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
        AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')
            BETWEEN '{date_str} 09:00:00' AND '{date_str} 13:30:00'
    GROUP BY ts_1s ORDER BY ts_1s
    """
    raw = query_ck(sql_fut)
    if not raw.strip():
        return None
    fut_df = pd.read_csv(StringIO(raw), sep="\t")
    if fut_df.empty or len(fut_df) < 100:
        return None

    mids_arr = fut_df["mid"].values
    fut_mid = np.mean(mids_arr)
    rets = np.diff(mids_arr) / mids_arr[:-1]
    rvol = float(np.std(rets) * np.sqrt(252 * len(rets)))  # annualized from 1s rets
    if pd.isna(fut_mid) or fut_mid <= 0:
        return None

    # Find nearest ATM strike within available range
    # Strikes use 50-pt increments near ATM, 100-pt further out
    atm_strike = round(fut_mid / 100) * 100

    # Clamp to available range
    atm_strike = max(series["strike_min"], min(series["strike_max"], atm_strike))

    best_straddle = None
    best_strike = atm_strike

    for offset in [0, -100, 100, -200, 200, -50, 50, -150, 150]:
        strike = int(atm_strike + offset)
        if strike < series["strike_min"] or strike > series["strike_max"]:
            continue
        call_sym = f"TXO{strike}{series['call']}"
        put_sym = f"TXO{strike}{series['put']}"

        sql_opt = f"""
        SELECT
            '{call_sym}' as call_sym,
            c_mid, c_bid, c_ask, p_mid, p_bid, p_ask
        FROM (
            SELECT
                avg((bids_price[1] + asks_price[1]) / 2) / {SCALE} as c_mid,
                avg(bids_price[1]) / {SCALE} as c_bid,
                avg(asks_price[1]) / {SCALE} as c_ask
            FROM hft.market_data
            WHERE symbol = '{call_sym}' AND type = 'BidAsk'
                AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
                AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
                AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')
                    BETWEEN '{date_str} 09:00:00' AND '{date_str} 13:30:00'
        ) c
        CROSS JOIN (
            SELECT
                avg((bids_price[1] + asks_price[1]) / 2) / {SCALE} as p_mid,
                avg(bids_price[1]) / {SCALE} as p_bid,
                avg(asks_price[1]) / {SCALE} as p_ask
            FROM hft.market_data
            WHERE symbol = '{put_sym}' AND type = 'BidAsk'
                AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
                AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
                AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')
                    BETWEEN '{date_str} 09:00:00' AND '{date_str} 13:30:00'
        ) p
        """
        try:
            raw_opt = query_ck(sql_opt)
            if not raw_opt.strip():
                continue
            opt_df = pd.read_csv(StringIO(raw_opt), sep="\t")
            if opt_df.empty:
                continue
            c_mid = opt_df["c_mid"].iloc[0]
            p_mid = opt_df["p_mid"].iloc[0]
            if pd.isna(c_mid) or pd.isna(p_mid) or c_mid <= 0 or p_mid <= 0:
                continue
            straddle = c_mid + p_mid
            if best_straddle is None or abs(strike - fut_mid) < abs(best_strike - fut_mid):
                best_straddle = straddle
                best_strike = strike
                best_c_bid = opt_df["c_bid"].iloc[0]
                best_c_ask = opt_df["c_ask"].iloc[0]
                best_p_bid = opt_df["p_bid"].iloc[0]
                best_p_ask = opt_df["p_ask"].iloc[0]
        except Exception:
            continue

    if best_straddle is None:
        return None

    iv = bsm_iv_approx(best_straddle, fut_mid, T)

    return {
        "date": date_str,
        "fut_sym": fut_sym,
        "fut_mid": fut_mid,
        "atm_strike": best_strike,
        "dte": dte,
        "straddle_mid": best_straddle,
        "c_bid": best_c_bid, "c_ask": best_c_ask,
        "p_bid": best_p_bid, "p_ask": best_p_ask,
        "iv": iv,
        "rv": rvol if not pd.isna(rvol) else 0,
        "vrp": iv - (rvol if not pd.isna(rvol) else 0),
    }


# ─────────────────────────────────────────────────────────────────────────
# Part 2: Straddle P&L Simulation
# ─────────────────────────────────────────────────────────────────────────

def load_futures_1s(date_str: str, fut_sym: str) -> pd.DataFrame:
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


def simulate_delta_hedge_day(
    fut_bars: pd.DataFrame,
    strike: float,
    iv: float,
    dte: int,
    c_bid: float, c_ask: float,
    p_bid: float, p_ask: float,
    hedge_interval_s: int,
    smart_hedge_fn=None,
) -> dict | None:
    """Simulate short straddle + delta hedge for one day.

    Entry: sell call at bid, sell put at bid (conservative)
    Delta hedge: buy/sell futures to maintain delta-neutral
    Exit: end of day mark-to-mid (or could hold to expiry)

    For simplicity: single-day simulation (entry at open, exit at close).
    """
    if len(fut_bars) < 100:
        return None

    ts = fut_bars["ts_1s"].values
    mids = fut_bars["mid"].values
    bids = fut_bars["bid"].values
    asks = fut_bars["ask"].values

    T_start = dte / 365.0
    session_secs = ts[-1] - ts[0]

    # Entry: sell straddle at bid prices
    straddle_premium = c_bid + p_bid  # what we receive (in option pts)
    entry_mid = mids[0]

    # Track: futures position (in lots), cumulative hedge cost
    fut_pos = 0.0  # fractional for calculation, round for execution
    hedge_cost_pts = 0.0  # in futures points
    n_hedges = 0
    last_hedge_ts = ts[0]

    # Initial delta hedge
    call_delta = bsm_delta(entry_mid, strike, T_start, iv, True)
    put_delta = bsm_delta(entry_mid, strike, T_start, iv, False)
    straddle_delta = -(call_delta + put_delta)  # short straddle, so negate
    target_pos = round(straddle_delta)  # hedge in whole lots

    if target_pos != 0:
        if target_pos > 0:
            hedge_cost_pts += target_pos * (asks[0] - mids[0])  # buy at ask
        else:
            hedge_cost_pts += abs(target_pos) * (mids[0] - bids[0])  # sell at bid
        hedge_cost_pts += abs(target_pos) * FUT_COMM_TAX_PTS
        fut_pos = target_pos
        n_hedges += 1

    # Intraday: rehedge at intervals
    for i in range(1, len(ts)):
        t = ts[i]
        elapsed_days = (t - ts[0]) / 86400.0
        T_now = max(T_start - elapsed_days, 1 / 365.0)  # at least 1 day

        should_hedge = False
        if smart_hedge_fn is not None:
            should_hedge = smart_hedge_fn(i, fut_bars)
        elif t - last_hedge_ts >= hedge_interval_s:
            should_hedge = True

        if should_hedge:
            spot = mids[i]
            cd = bsm_delta(spot, strike, T_now, iv, True)
            pd_ = bsm_delta(spot, strike, T_now, iv, False)
            new_delta = -(cd + pd_)
            new_target = round(new_delta)
            trade_lots = new_target - round(fut_pos)

            if trade_lots != 0:
                if trade_lots > 0:
                    hedge_cost_pts += trade_lots * (asks[i] - mids[i])
                else:
                    hedge_cost_pts += abs(trade_lots) * (mids[i] - bids[i])
                hedge_cost_pts += abs(trade_lots) * FUT_COMM_TAX_PTS
                fut_pos = new_target
                n_hedges += 1
                last_hedge_ts = t

    # Exit: close futures position at end of day
    exit_mid = mids[-1]
    if round(fut_pos) != 0:
        lots = round(fut_pos)
        if lots > 0:
            close_price = bids[-1]
        else:
            close_price = asks[-1]
        hedge_cost_pts += abs(lots) * abs(close_price - exit_mid)
        hedge_cost_pts += abs(lots) * FUT_COMM_TAX_PTS

    # Futures P&L from position (mark-to-market)
    fut_pnl_pts = fut_pos * (exit_mid - entry_mid)

    # Option P&L: premium received - intrinsic at exit
    call_intrinsic = max(exit_mid - strike, 0)
    put_intrinsic = max(strike - exit_mid, 0)
    # Rough theta: for 1 day, option decays by ~ straddle_premium / dte
    # But more accurately: BSM value at exit vs entry
    # Simplified: option_pnl = premium_received - intrinsic_at_exit - remaining_time_value
    # For single-day sim, approximate time value remaining ≈ straddle * (dte-1)/dte
    remaining_tv = straddle_premium * max(dte - 1, 0) / max(dte, 1)
    option_pnl_pts = straddle_premium - (call_intrinsic + put_intrinsic) - remaining_tv
    # This simplifies to: option_pnl ≈ straddle_premium / dte (daily theta)
    daily_theta = straddle_premium / max(dte, 1)

    # Gamma P&L (realized from futures movement)
    # gamma_pnl = -0.5 * gamma * (price_move)^2 (we're short gamma)
    straddle_gamma = 2 * bsm_gamma(entry_mid, strike, T_start, iv)
    day_move = exit_mid - entry_mid
    gamma_loss = 0.5 * straddle_gamma * day_move**2

    # Total P&L in NTD
    theta_ntd = daily_theta * OPT_MULT
    gamma_loss_ntd = gamma_loss * OPT_MULT
    hedge_cost_ntd = hedge_cost_pts * FUT_MULT
    # Add option commission
    opt_comm_ntd = OPT_COMM_NTD * 2  # 2 legs

    net_pnl_ntd = theta_ntd - gamma_loss_ntd - hedge_cost_ntd - opt_comm_ntd

    return {
        "daily_theta_pts": daily_theta,
        "gamma_loss_pts": gamma_loss,
        "hedge_cost_pts": hedge_cost_pts,
        "n_hedges": n_hedges,
        "day_move_pts": abs(day_move),
        "theta_ntd": theta_ntd,
        "gamma_loss_ntd": gamma_loss_ntd,
        "hedge_cost_ntd": hedge_cost_ntd,
        "net_pnl_ntd": net_pnl_ntd,
    }


# ─────────────────────────────────────────────────────────────────────────
# Part 3: Smart Hedge Functions (using LOB features)
# ─────────────────────────────────────────────────────────────────────────

def make_smart_hedge_fn(base_interval: int, vol_threshold: float = 1.5):
    """Smart hedge: hedge more often when price is moving fast, less when calm.

    Uses rolling 1-min realized vol vs session average to decide.
    Hedge when: (a) base interval elapsed AND vol is high, OR
                (b) 2x base interval elapsed regardless.
    """
    def fn(i: int, bars: pd.DataFrame) -> bool:
        ts = bars["ts_1s"].values
        mids = bars["mid"].values

        # Always check minimum interval
        if not hasattr(fn, "_last_t"):
            fn._last_t = ts[0]

        elapsed = ts[i] - fn._last_t

        # Compute recent 60s vol
        lookback = min(i, 60)
        if lookback < 10:
            if elapsed >= base_interval:
                fn._last_t = ts[i]
                return True
            return False

        recent_rets = np.diff(mids[i - lookback:i + 1]) / mids[i - lookback:i]
        recent_vol = np.std(recent_rets) if len(recent_rets) > 1 else 0

        # Session vol (expanding window)
        all_rets = np.diff(mids[:i + 1]) / mids[:i]
        session_vol = np.std(all_rets) if len(all_rets) > 10 else recent_vol

        vol_ratio = recent_vol / session_vol if session_vol > 0 else 1.0

        # High vol: hedge at base interval
        # Low vol: hedge at 3x base interval
        if vol_ratio > vol_threshold and elapsed >= base_interval:
            fn._last_t = ts[i]
            return True
        elif elapsed >= base_interval * 3:
            fn._last_t = ts[i]
            return True

        return False

    return fn


def main():
    dates = sorted(FRONT_MONTH_FUT.keys())

    # ═══════════════════════════════════════════════════════════════════
    # Part 1: IV vs RV
    # ═══════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("PART 1: Daily IV vs RV")
    print("=" * 80)

    iv_rv_data = []
    for d in dates:
        print(f"  {d}...", end=" ", flush=True)
        try:
            r = compute_daily_iv_rv(d)
            if r:
                iv_rv_data.append(r)
                print(f"IV={r['iv']:.1%} RV={r['rv']:.1%} VRP={r['vrp']:+.1%} "
                      f"DTE={r['dte']} straddle={r['straddle_mid']:.0f}")
            else:
                print("SKIP")
        except Exception as e:
            print(f"ERROR: {str(e)[:60]}")

    if iv_rv_data:
        ivs = [r["iv"] for r in iv_rv_data if r["iv"] > 0]
        rvs = [r["rv"] for r in iv_rv_data if r["rv"] > 0]
        vrps = [r["vrp"] for r in iv_rv_data if r["iv"] > 0 and r["rv"] > 0]

        print(f"\n  Summary ({len(iv_rv_data)} days):")
        print(f"    IV:  mean={np.mean(ivs):.1%} median={np.median(ivs):.1%} "
              f"std={np.std(ivs):.1%}")
        print(f"    RV:  mean={np.mean(rvs):.1%} median={np.median(rvs):.1%} "
              f"std={np.std(rvs):.1%}")
        print(f"    VRP: mean={np.mean(vrps):+.1%} median={np.median(vrps):+.1%} "
              f"positive={sum(1 for v in vrps if v > 0)}/{len(vrps)} days")

    # ═══════════════════════════════════════════════════════════════════
    # Part 2: Straddle + Delta Hedge Simulation
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("PART 2: Short Straddle + Delta Hedge Simulation")
    print("=" * 80)

    hedge_intervals = [60, 300, 900, 1800]  # 1min, 5min, 15min, 30min
    interval_results = {h: [] for h in hedge_intervals}
    smart_results = []

    for r in iv_rv_data:
        d = r["date"]
        fut_sym = r["fut_sym"]

        fut_bars = load_futures_1s(d, fut_sym)
        if len(fut_bars) < 500:
            continue

        print(f"\n  {d} (IV={r['iv']:.0%} RV={r['rv']:.0%} DTE={r['dte']}):")

        # Fixed-frequency hedges
        for interval in hedge_intervals:
            sim = simulate_delta_hedge_day(
                fut_bars, r["atm_strike"], r["iv"], r["dte"],
                r["c_bid"], r["c_ask"], r["p_bid"], r["p_ask"],
                hedge_interval_s=interval,
            )
            if sim:
                interval_results[interval].append({**sim, "date": d})
                tag = f"{interval//60}min"
                print(f"    {tag:>5s}: theta={sim['daily_theta_pts']:+.0f} "
                      f"gamma={sim['gamma_loss_pts']:.0f} "
                      f"hedge_cost={sim['hedge_cost_pts']:.0f} "
                      f"n_hedge={sim['n_hedges']:>3d} "
                      f"net={sim['net_pnl_ntd']:+,.0f} NTD")

        # Smart hedge (base=5min, adapt by vol)
        smart_fn = make_smart_hedge_fn(300, 1.5)
        sim_smart = simulate_delta_hedge_day(
            fut_bars, r["atm_strike"], r["iv"], r["dte"],
            r["c_bid"], r["c_ask"], r["p_bid"], r["p_ask"],
            hedge_interval_s=99999,  # ignored, using smart_fn
            smart_hedge_fn=smart_fn,
        )
        if sim_smart:
            smart_results.append({**sim_smart, "date": d})
            print(f"    smart: theta={sim_smart['daily_theta_pts']:+.0f} "
                  f"gamma={sim_smart['gamma_loss_pts']:.0f} "
                  f"hedge_cost={sim_smart['hedge_cost_pts']:.0f} "
                  f"n_hedge={sim_smart['n_hedges']:>3d} "
                  f"net={sim_smart['net_pnl_ntd']:+,.0f} NTD")

    # ═══════════════════════════════════════════════════════════════════
    # Part 3: Summary Comparison
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("PART 3: Strategy Comparison")
    print("=" * 80)

    print(f"\n{'Strategy':<15s} {'Days':>4s} {'AvgTheta':>9s} {'AvgGamma':>9s} "
          f"{'AvgHCost':>9s} {'AvgHedge':>8s} {'AvgNet':>10s} {'TotNet':>12s} {'WR':>5s}")
    print("-" * 90)

    all_strategies = {}
    for interval in hedge_intervals:
        data = interval_results[interval]
        if not data:
            continue
        label = f"Fixed {interval//60}min"
        nets = [d["net_pnl_ntd"] for d in data]
        all_strategies[label] = nets
        print(f"{label:<15s} {len(data):>4d} "
              f"{np.mean([d['daily_theta_pts'] for d in data]):>+9.0f} "
              f"{np.mean([d['gamma_loss_pts'] for d in data]):>9.0f} "
              f"{np.mean([d['hedge_cost_pts'] for d in data]):>9.0f} "
              f"{np.mean([d['n_hedges'] for d in data]):>8.1f} "
              f"{np.mean(nets):>+10,.0f} "
              f"{np.sum(nets):>+12,.0f} "
              f"{sum(1 for n in nets if n > 0)/len(nets):>5.0%}")

    if smart_results:
        label = "Smart 5min"
        nets = [d["net_pnl_ntd"] for d in smart_results]
        all_strategies[label] = nets
        print(f"{label:<15s} {len(smart_results):>4d} "
              f"{np.mean([d['daily_theta_pts'] for d in smart_results]):>+9.0f} "
              f"{np.mean([d['gamma_loss_pts'] for d in smart_results]):>9.0f} "
              f"{np.mean([d['hedge_cost_pts'] for d in smart_results]):>9.0f} "
              f"{np.mean([d['n_hedges'] for d in smart_results]):>8.1f} "
              f"{np.mean(nets):>+10,.0f} "
              f"{np.sum(nets):>+12,.0f} "
              f"{sum(1 for n in nets if n > 0)/len(nets):>5.0%}")

    # Best strategy
    if all_strategies:
        best = max(all_strategies.items(), key=lambda x: np.mean(x[1]))
        print(f"\n  BEST: {best[0]} (avg {np.mean(best[1]):+,.0f} NTD/day)")

        # Sharpe
        nets_arr = np.array(best[1])
        if len(nets_arr) >= 2 and nets_arr.std() > 0:
            daily_sharpe = nets_arr.mean() / nets_arr.std()
            ann_sharpe = daily_sharpe * math.sqrt(252)
            print(f"  Daily Sharpe: {daily_sharpe:.2f}, Annualized: {ann_sharpe:.2f}")

        # Split train/holdout
        n = len(nets_arr)
        if n >= 10:
            train = nets_arr[:n - 5]
            holdout = nets_arr[n - 5:]
            print(f"\n  Train ({len(train)} days): "
                  f"mean={train.mean():+,.0f} NTD, wr={sum(train > 0)/len(train):.0%}")
            print(f"  Holdout ({len(holdout)} days): "
                  f"mean={holdout.mean():+,.0f} NTD, wr={sum(holdout > 0)/len(holdout):.0%}")

            verdict = holdout.mean() > 0
            print(f"\n  VERDICT: {'PROMISING' if verdict else 'FAIL'} "
                  f"(holdout avg={holdout.mean():+,.0f} NTD/day)")


if __name__ == "__main__":
    main()
