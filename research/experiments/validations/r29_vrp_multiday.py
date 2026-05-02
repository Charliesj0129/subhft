"""R29 VRP Multi-Day Strategy Evaluation (v2)

Improvements over v1:
1. Multi-day straddle holding (entry → expiry or roll)
2. Full BSM mark-to-market (not linear theta approx)
3. Overnight gap risk: unhedged gamma exposure at open
4. Option spread cost properly accounted (sell at bid, buy back at ask)
5. D4 regime-adaptive hedge frequency
6. Stress test: worst-case gap scenarios

Conservative execution throughout:
- Options: sell at bid, buy at ask
- Futures: buy at ask, sell at bid + comm/tax
"""

import math
import subprocess
import numpy as np
import pandas as pd
from io import StringIO
from datetime import datetime, timedelta
from scipy.stats import norm

SCALE = 1_000_000
OPT_MULT = 50      # NTD per option point
FUT_MULT = 200      # NTD per futures point
FUT_COMM_TAX_PTS = 2.3  # futures RT comm+tax in pts
OPT_COMM_NTD = 40   # option RT comm per leg in NTD

# Same option series and futures from v1
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

HOLDOUT_DAYS = 5
RISK_FREE = 0.015  # ~1.5% risk-free rate (Taiwan)


# ─────────────────────────────────────────────────────────────
# BSM Functions
# ─────────────────────────────────────────────────────────────

def bsm_d1(S: float, K: float, T: float, vol: float, r: float = RISK_FREE) -> float:
    if T <= 0 or vol <= 0 or S <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * vol**2) * T) / (vol * math.sqrt(T))


def bsm_price(S: float, K: float, T: float, vol: float, is_call: bool, r: float = RISK_FREE) -> float:
    """BSM option price."""
    if T <= 0:
        return max(S - K, 0) if is_call else max(K - S, 0)
    d1 = bsm_d1(S, K, T, vol, r)
    d2 = d1 - vol * math.sqrt(T)
    if is_call:
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bsm_delta(S: float, K: float, T: float, vol: float, is_call: bool, r: float = RISK_FREE) -> float:
    if T <= 0:
        if is_call:
            return 1.0 if S > K else 0.0
        else:
            return -1.0 if S < K else 0.0
    d1 = bsm_d1(S, K, T, vol, r)
    return norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0


def bsm_gamma(S: float, K: float, T: float, vol: float, r: float = RISK_FREE) -> float:
    if T <= 0 or vol <= 0 or S <= 0:
        return 0.0
    d1 = bsm_d1(S, K, T, vol, r)
    return norm.pdf(d1) / (S * vol * math.sqrt(T))


def bsm_vega(S: float, K: float, T: float, vol: float, r: float = RISK_FREE) -> float:
    if T <= 0 or vol <= 0 or S <= 0:
        return 0.0
    d1 = bsm_d1(S, K, T, vol, r)
    return S * norm.pdf(d1) * math.sqrt(T)


def bsm_iv_approx(straddle_price: float, spot: float, T: float) -> float:
    """Brenner-Subrahmanyam approximation."""
    if T <= 0 or spot <= 0:
        return 0.0
    return straddle_price / (spot * math.sqrt(T)) * math.sqrt(2 * math.pi)


# ─────────────────────────────────────────────────────────────
# Data Loading
# ─────────────────────────────────────────────────────────────

def query_ck(sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client",
         "--query", sql, "--format", "TabSeparatedWithNames"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    return result.stdout


def get_option_series(date_str: str) -> dict | None:
    for series in OPTION_SERIES.values():
        if series["dates_from"] <= date_str <= series["dates_to"]:
            return series
    return None


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


def load_option_quote(date_str: str, strike: int, series: dict) -> dict | None:
    """Load daily average bid/ask for call and put at given strike."""
    call_sym = f"TXO{strike}{series['call']}"
    put_sym = f"TXO{strike}{series['put']}"

    sql = f"""
    SELECT
        c_bid, c_ask, c_mid, p_bid, p_ask, p_mid
    FROM (
        SELECT
            avg(bids_price[1]) / {SCALE} as c_bid,
            avg(asks_price[1]) / {SCALE} as c_ask,
            avg((bids_price[1] + asks_price[1]) / 2) / {SCALE} as c_mid
        FROM hft.market_data
        WHERE symbol = '{call_sym}' AND type = 'BidAsk'
            AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
            AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
            AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')
                BETWEEN '{date_str} 09:00:00' AND '{date_str} 13:30:00'
    ) c
    CROSS JOIN (
        SELECT
            avg(bids_price[1]) / {SCALE} as p_bid,
            avg(asks_price[1]) / {SCALE} as p_ask,
            avg((bids_price[1] + asks_price[1]) / 2) / {SCALE} as p_mid
        FROM hft.market_data
        WHERE symbol = '{put_sym}' AND type = 'BidAsk'
            AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
            AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
            AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')
                BETWEEN '{date_str} 09:00:00' AND '{date_str} 13:30:00'
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
# D4 Regime Features (Opening Period)
# ─────────────────────────────────────────────────────────────

def compute_opening_features(fut_bars: pd.DataFrame) -> dict:
    """Compute D4 regime features from first 30min of trading."""
    ts = fut_bars["ts_1s"].values
    mids = fut_bars["mid"].values

    start_ts = ts[0]
    open_end = start_ts + 1800  # 30 min

    open_mask = ts < open_end
    n_open = open_mask.sum()
    if n_open < 30:
        return {"open_abs_ret": 0.0, "open_volatility": 0.0, "open_range": 0.0}

    open_mids = mids[:n_open]

    # Absolute return during opening
    abs_ret = abs((open_mids[-1] - open_mids[0]) / open_mids[0]) if open_mids[0] > 0 else 0

    # Opening range
    open_range = (np.max(open_mids) - np.min(open_mids))

    # 5-min bar volatility
    unique_5min = np.unique((ts[:n_open] - start_ts) // 300)
    bar_closes = []
    for b5 in unique_5min:
        mask_5 = ((ts[:n_open] - start_ts) // 300) == b5
        if mask_5.any():
            bar_closes.append(open_mids[mask_5][-1])
    bar_arr = np.array(bar_closes)
    open_vol = float(np.std(np.diff(bar_arr) / bar_arr[:-1])) if len(bar_arr) >= 3 else 0.0

    return {
        "open_abs_ret": abs_ret,
        "open_volatility": open_vol,
        "open_range": open_range,
    }


# ─────────────────────────────────────────────────────────────
# Multi-Day Straddle Simulation
# ─────────────────────────────────────────────────────────────

def simulate_multiday_straddle(
    daily_data: list[dict],
    hedge_mode: str = "smart",  # "fixed_Xmin" or "smart"
    smart_base_s: int = 300,
    smart_vol_thresh: float = 1.5,
    d4_adaptive: bool = False,
    flatten_overnight: bool = True,
) -> dict:
    """Simulate holding a short straddle across multiple days.

    Correct P&L accounting:
    - Option MtM: BSM value change (short position profits from decay)
    - Hedge MtM: futures position mark-to-market
    - Hedge cost: slippage (bid-ask) + commissions on each rebalance
    - Daily net = Option MtM + Hedge MtM - Hedge cost

    Position lifecycle:
    - Enter on first day: sell call+put at bid
    - Hold through subsequent days with delta hedging
    - Exit on last day: buy back at ask (or settle at intrinsic)
    """
    if not daily_data:
        return {}

    n_days = len(daily_data)
    strike = daily_data[0]["atm_strike"]
    iv = daily_data[0]["iv"]
    series_expiry = daily_data[0]["expiry"]

    # Entry: sell straddle at bid on day 1
    d0 = daily_data[0]
    premium_received = d0["c_bid"] + d0["p_bid"]  # pts
    entry_spot = d0["open_mid"]

    daily_pnls = []
    prev_close_mid = entry_spot
    fut_pos = 0  # current futures position (lots)
    total_hedges = 0
    cum_hedge_slippage_ntd = 0.0
    cum_hedge_mtm_ntd = 0.0

    for day_idx, day in enumerate(daily_data):
        d_str = day["date"]
        fut_bars = day["fut_bars"]
        dte = day["dte"]
        T = dte / 365.0

        ts = fut_bars["ts_1s"].values
        mids = fut_bars["mid"].values
        bids = fut_bars["bid"].values
        asks = fut_bars["ask"].values

        open_mid = mids[0]
        close_mid = mids[-1]
        T_end = max((dte - 1) / 365.0, 0)

        day_hedge_slip_ntd = 0.0  # slippage + commissions
        day_hedge_mtm_ntd = 0.0   # futures position P&L

        # ── Option mark-to-market ──
        # BSM value at start of day vs end of day
        # We are SHORT the straddle, so profit = value_start - value_end
        opt_val_open = bsm_price(open_mid, strike, T, iv, True) + \
                       bsm_price(open_mid, strike, T, iv, False)
        opt_val_close = bsm_price(close_mid, strike, T_end, iv, True) + \
                        bsm_price(close_mid, strike, T_end, iv, False)
        opt_mtm_ntd = (opt_val_open - opt_val_close) * OPT_MULT

        # For decomposition: pure theta and gamma components
        opt_val_close_same_spot = bsm_price(open_mid, strike, T_end, iv, True) + \
                                  bsm_price(open_mid, strike, T_end, iv, False)
        pure_theta_ntd = (opt_val_open - opt_val_close_same_spot) * OPT_MULT
        # gamma + higher order = option MtM - theta
        gamma_component_ntd = opt_mtm_ntd - pure_theta_ntd  # negative when spot moves

        # ── Overnight gap: option value jumps at open ──
        overnight_gap_ntd = 0.0
        if day_idx > 0:
            gap = open_mid - prev_close_mid
            # Option value change from overnight gap (we're short)
            prev_T = daily_data[day_idx - 1]["dte"] / 365.0
            prev_T_end = T  # T at open today
            val_prev_close = bsm_price(prev_close_mid, strike, prev_T_end, iv, True) + \
                             bsm_price(prev_close_mid, strike, prev_T_end, iv, False)
            val_open = bsm_price(open_mid, strike, T, iv, True) + \
                       bsm_price(open_mid, strike, T, iv, False)
            # Overnight option MtM (short position)
            overnight_gap_ntd = (val_prev_close - val_open) * OPT_MULT

            # If we carried futures overnight, they also have MtM
            if not flatten_overnight and fut_pos != 0:
                day_hedge_mtm_ntd += fut_pos * (open_mid - prev_close_mid) * FUT_MULT

        # ── Delta hedging ──
        # Determine hedge interval
        if hedge_mode.startswith("fixed_"):
            interval_s = int(hedge_mode.split("_")[1].replace("min", "")) * 60
        else:
            interval_s = smart_base_s

        if d4_adaptive:
            feats = day.get("open_features", {})
            open_vol = feats.get("open_volatility", 0)
            if open_vol > 0.003:
                interval_s = max(interval_s // 2, 60)
            elif open_vol < 0.001:
                interval_s = min(interval_s * 2, 3600)

        # Rehedge at open (especially after overnight gap)
        cd = bsm_delta(open_mid, strike, T, iv, True)
        pd_ = bsm_delta(open_mid, strike, T, iv, False)
        target = round(-(cd + pd_))
        trade = target - fut_pos
        if trade != 0:
            slip = trade * (asks[0] - mids[0]) if trade > 0 else abs(trade) * (mids[0] - bids[0])
            day_hedge_slip_ntd += slip * FUT_MULT + abs(trade) * FUT_COMM_TAX_PTS * FUT_MULT
            fut_pos = target
            total_hedges += 1

        last_hedge_ts = ts[0]
        # Track hedge position value at start of each segment for MtM
        hedge_entry_price = open_mid

        for i in range(1, len(ts)):
            t = ts[i]
            elapsed_days = (t - ts[0]) / 86400.0
            T_now = max(T - elapsed_days, 1 / 365.0)

            should_hedge = False
            if hedge_mode == "smart":
                elapsed = t - last_hedge_ts
                lookback = min(i, 60)
                if lookback < 10:
                    should_hedge = elapsed >= interval_s
                else:
                    recent_rets = np.diff(mids[i - lookback:i + 1]) / mids[i - lookback:i]
                    recent_vol = float(np.std(recent_rets)) if len(recent_rets) > 1 else 0
                    all_rets = np.diff(mids[:i + 1]) / mids[:i]
                    session_vol = float(np.std(all_rets)) if len(all_rets) > 10 else recent_vol
                    vol_ratio = recent_vol / session_vol if session_vol > 0 else 1.0
                    if vol_ratio > smart_vol_thresh and elapsed >= interval_s:
                        should_hedge = True
                    elif elapsed >= interval_s * 3:
                        should_hedge = True
            else:
                should_hedge = (t - last_hedge_ts) >= interval_s

            if should_hedge:
                spot = mids[i]
                # MtM on current position before rebalance
                day_hedge_mtm_ntd += fut_pos * (spot - hedge_entry_price) * FUT_MULT
                hedge_entry_price = spot

                cd = bsm_delta(spot, strike, T_now, iv, True)
                pd_ = bsm_delta(spot, strike, T_now, iv, False)
                new_target = round(-(cd + pd_))
                trade = new_target - fut_pos

                if trade != 0:
                    slip = trade * (asks[i] - mids[i]) if trade > 0 else abs(trade) * (mids[i] - bids[i])
                    day_hedge_slip_ntd += slip * FUT_MULT + abs(trade) * FUT_COMM_TAX_PTS * FUT_MULT
                    fut_pos = new_target
                    total_hedges += 1
                    last_hedge_ts = t

        # Final MtM to close
        day_hedge_mtm_ntd += fut_pos * (close_mid - hedge_entry_price) * FUT_MULT

        # ── End-of-day: flatten futures if configured ──
        if flatten_overnight and fut_pos != 0:
            lots = fut_pos
            slip = lots * (mids[-1] - bids[-1]) if lots > 0 else abs(lots) * (asks[-1] - mids[-1])
            day_hedge_slip_ntd += slip * FUT_MULT + abs(lots) * FUT_COMM_TAX_PTS * FUT_MULT
            fut_pos = 0
            total_hedges += 1

        cum_hedge_slippage_ntd += day_hedge_slip_ntd
        cum_hedge_mtm_ntd += day_hedge_mtm_ntd

        # Daily net = option MtM + overnight option MtM + hedge MtM - hedge costs
        day_net = opt_mtm_ntd + overnight_gap_ntd + day_hedge_mtm_ntd - day_hedge_slip_ntd
        daily_pnls.append({
            "date": d_str,
            "dte": dte,
            "opt_mtm_ntd": opt_mtm_ntd,
            "theta_ntd": pure_theta_ntd,
            "gamma_ntd": gamma_component_ntd,
            "overnight_ntd": overnight_gap_ntd,
            "hedge_mtm_ntd": day_hedge_mtm_ntd,
            "hedge_slip_ntd": day_hedge_slip_ntd,
            "net_ntd": day_net,
            "open": open_mid,
            "close": close_mid,
            "gap": open_mid - prev_close_mid if day_idx > 0 else 0,
        })

        prev_close_mid = close_mid

    # ── Exit cost: buy back straddle at ask on last day ──
    last_day = daily_data[-1]
    is_expiry = last_day["date"] == series_expiry
    if is_expiry:
        exit_cost_pts = max(last_day["close_mid"] - strike, 0) + \
                        max(strike - last_day["close_mid"], 0)
    else:
        exit_cost_pts = last_day.get("c_ask", 0) + last_day.get("p_ask", 0)

    option_net_pts = premium_received - exit_cost_pts
    option_net_ntd = option_net_pts * OPT_MULT
    opt_comm_total = OPT_COMM_NTD * 4  # 2 legs × entry + exit

    # Total = option net (entry-exit) + hedge MtM - hedge slippage - option comm
    total_net_ntd = option_net_ntd + cum_hedge_mtm_ntd - cum_hedge_slippage_ntd - opt_comm_total

    # Also compute from daily sums (should match closely)
    daily_sum = sum(d["net_ntd"] for d in daily_pnls)
    # Difference = entry/exit spread effect not captured in daily MtM
    spread_entry_cost = (premium_received - (d0["c_bid"] + d0["c_ask"])/2 -
                         (d0["p_bid"] + d0["p_ask"])/2 + premium_received) * OPT_MULT / 2
    # Note: daily MtM uses BSM model, not actual market quotes for intermediate days

    nets = np.array([d["net_ntd"] for d in daily_pnls])
    sharpe = 0.0
    if len(nets) >= 2 and nets.std() > 0:
        sharpe = nets.mean() / nets.std() * math.sqrt(252)

    return {
        "n_days": n_days,
        "strike": strike,
        "premium_received_pts": premium_received,
        "exit_cost_pts": exit_cost_pts,
        "option_net_pts": option_net_pts,
        "option_net_ntd": option_net_ntd,
        "cum_hedge_mtm_ntd": cum_hedge_mtm_ntd,
        "cum_hedge_slippage_ntd": cum_hedge_slippage_ntd,
        "opt_comm_ntd": opt_comm_total,
        "total_net_ntd": total_net_ntd,
        "daily_sum_ntd": daily_sum,
        "total_hedges": total_hedges,
        "daily_pnls": daily_pnls,
        "sharpe": sharpe,
        "win_rate": sum(1 for d in daily_pnls if d["net_ntd"] > 0) / len(daily_pnls),
        "max_daily_loss": min(d["net_ntd"] for d in daily_pnls),
        "max_daily_gain": max(d["net_ntd"] for d in daily_pnls),
        "avg_daily_pnl": float(np.mean(nets)),
    }


# ─────────────────────────────────────────────────────────────
# Main Pipeline
# ─────────────────────────────────────────────────────────────

def main():
    dates = sorted(FRONT_MONTH_FUT.keys())

    # ═══════════════════════════════════════════════════════════
    # Step 1: Load all data
    # ═══════════════════════════════════════════════════════════
    print("=" * 90)
    print("R29 VRP Multi-Day Strategy (v2)")
    print("=" * 90)
    print("\nLoading data...")

    all_days = []
    for d in dates:
        series = get_option_series(d)
        fut_sym = FRONT_MONTH_FUT[d]
        if not series:
            print(f"  {d}: no option series, SKIP")
            continue

        exp = datetime.strptime(series["expiry"], "%Y-%m-%d")
        today = datetime.strptime(d, "%Y-%m-%d")
        dte = (exp - today).days
        if dte <= 0:
            print(f"  {d}: DTE=0, SKIP")
            continue

        print(f"  {d} ({fut_sym}, DTE={dte})...", end=" ", flush=True)

        # Load futures bars
        fut_bars = load_futures_1s(d, fut_sym)
        if len(fut_bars) < 500:
            print(f"SKIP (only {len(fut_bars)} bars)")
            continue

        open_mid = fut_bars["mid"].iloc[0]
        close_mid = fut_bars["mid"].iloc[-1]

        # Find ATM strike
        atm_strike = round(open_mid / 100) * 100
        atm_strike = max(series["strike_min"], min(series["strike_max"], atm_strike))

        # Try nearest strikes
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

        # Compute IV
        T = dte / 365.0
        straddle_mid = best_opt["c_mid"] + best_opt["p_mid"]
        iv = bsm_iv_approx(straddle_mid, open_mid, T)

        # Compute RV from 1s bars
        mids_arr = fut_bars["mid"].values
        rets = np.diff(mids_arr) / mids_arr[:-1]
        rv = float(np.std(rets) * np.sqrt(252 * len(rets)))

        # Opening features (D4)
        open_feats = compute_opening_features(fut_bars)

        spread_c = best_opt["c_ask"] - best_opt["c_bid"]
        spread_p = best_opt["p_ask"] - best_opt["p_bid"]

        day_data = {
            "date": d,
            "fut_sym": fut_sym,
            "expiry": series["expiry"],
            "series_key": [k for k, v in OPTION_SERIES.items()
                           if v["dates_from"] <= d <= v["dates_to"]][0],
            "dte": dte,
            "atm_strike": best_strike,
            "iv": iv,
            "rv": rv,
            "vrp": iv - rv,
            "open_mid": open_mid,
            "close_mid": close_mid,
            "c_bid": best_opt["c_bid"],
            "c_ask": best_opt["c_ask"],
            "p_bid": best_opt["p_bid"],
            "p_ask": best_opt["p_ask"],
            "straddle_mid": straddle_mid,
            "spread_c": spread_c,
            "spread_p": spread_p,
            "fut_bars": fut_bars,
            "open_features": open_feats,
        }
        all_days.append(day_data)
        print(f"OK  IV={iv:.0%} RV={rv:.0%} VRP={iv-rv:+.0%} straddle={straddle_mid:.0f} "
              f"K={best_strike} spread_c={spread_c:.0f} spread_p={spread_p:.0f}")

    print(f"\n{len(all_days)} usable days loaded")

    # ═══════════════════════════════════════════════════════════
    # Step 2: IV vs RV Summary
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print("IV vs RV Summary")
    print("=" * 90)

    ivs = [d["iv"] for d in all_days]
    rvs = [d["rv"] for d in all_days]
    vrps = [d["vrp"] for d in all_days]
    print(f"  IV:  mean={np.mean(ivs):.1%} std={np.std(ivs):.1%}")
    print(f"  RV:  mean={np.mean(rvs):.1%} std={np.std(rvs):.1%}")
    print(f"  VRP: mean={np.mean(vrps):+.1%} positive={sum(1 for v in vrps if v > 0)}/{len(vrps)}")

    # ═══════════════════════════════════════════════════════════
    # Step 3: Multi-Day Simulation by Option Series
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print("Multi-Day Straddle Simulation")
    print("=" * 90)

    # Group by series
    series_groups = {}
    for d in all_days:
        sk = d["series_key"]
        series_groups.setdefault(sk, []).append(d)

    hedge_modes = ["fixed_1min", "fixed_5min", "fixed_15min", "fixed_30min", "smart"]

    all_series_results = []

    for sk, days in sorted(series_groups.items()):
        print(f"\n  Series {sk}: {len(days)} days ({days[0]['date']} to {days[-1]['date']}), "
              f"expiry={days[0]['expiry']}")

        for mode in hedge_modes:
            result = simulate_multiday_straddle(
                days,
                hedge_mode=mode,
                smart_base_s=300,
                smart_vol_thresh=1.5,
                d4_adaptive=False,
                flatten_overnight=True,
            )

            if not result:
                continue

            tag = mode.replace("fixed_", "").replace("min", "m")
            print(f"    {tag:>6s}: "
                  f"opt_net={result['option_net_ntd']:>+9,.0f} "
                  f"hedge_mtm={result['cum_hedge_mtm_ntd']:>+9,.0f} "
                  f"hedge_slip={result['cum_hedge_slippage_ntd']:>9,.0f} "
                  f"comm={result['opt_comm_ntd']:>5,.0f} "
                  f"TOTAL={result['total_net_ntd']:>+10,.0f} "
                  f"hedges={result['total_hedges']:>4d}")

            all_series_results.append({
                "series": sk,
                "mode": mode,
                **result,
            })

    # ═══════════════════════════════════════════════════════════
    # Step 4: D4-Adaptive Hedging
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print("D4-Adaptive Hedging (smart + regime filter)")
    print("=" * 90)

    for sk, days in sorted(series_groups.items()):
        for flat_label, flat_val in [("flatten", True), ("carry", False)]:
            result_d4 = simulate_multiday_straddle(
                days,
                hedge_mode="smart",
                smart_base_s=300,
                smart_vol_thresh=1.5,
                d4_adaptive=True,
                flatten_overnight=flat_val,
            )
            if result_d4:
                print(f"  {sk} D4+{flat_label}: net={result_d4['total_net_ntd']:>+10,.0f} NTD "
                      f"hedges={result_d4['total_hedges']}")

    # ═══════════════════════════════════════════════════════════
    # Step 5: Aggregate Results + Train/Holdout
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print("Aggregate Strategy Comparison")
    print("=" * 90)

    # Combine daily pnls across series for each hedge mode
    for mode in hedge_modes:
        mode_daily = []
        for sr in all_series_results:
            if sr["mode"] == mode:
                mode_daily.extend(sr["daily_pnls"])

        if not mode_daily:
            continue

        mode_daily.sort(key=lambda x: x["date"])
        nets = np.array([d["net_ntd"] for d in mode_daily])

        # Train/holdout split
        n = len(nets)
        split = max(n - HOLDOUT_DAYS, 1)
        train = nets[:split]
        holdout = nets[split:]

        tag = mode.replace("fixed_", "").replace("min", "m")
        print(f"\n  {tag}:")
        print(f"    ALL ({n} days):     mean={nets.mean():>+8,.0f} NTD "
              f"wr={sum(nets > 0)/n:.0%} "
              f"sharpe={nets.mean()/nets.std()*math.sqrt(252) if nets.std() > 0 else 0:.1f} "
              f"maxDD={min(nets):>+8,.0f}")

        if len(train) >= 3:
            print(f"    TRAIN ({len(train)} days):  mean={train.mean():>+8,.0f} NTD "
                  f"wr={sum(train > 0)/len(train):.0%}")
        if len(holdout) >= 1:
            print(f"    HOLDOUT ({len(holdout)} days): mean={holdout.mean():>+8,.0f} NTD "
                  f"wr={sum(holdout > 0)/len(holdout):.0%}")

    # ═══════════════════════════════════════════════════════════
    # Step 6: Stress Test — Worst Gaps
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print("Stress Test: Overnight Gap Analysis")
    print("=" * 90)

    gaps = []
    for i in range(1, len(all_days)):
        gap = all_days[i]["open_mid"] - all_days[i - 1]["close_mid"]
        gaps.append({
            "date": all_days[i]["date"],
            "gap_pts": gap,
            "abs_gap": abs(gap),
            "prev_close": all_days[i - 1]["close_mid"],
            "dte": all_days[i]["dte"],
        })

    if gaps:
        gaps.sort(key=lambda x: x["abs_gap"], reverse=True)
        print(f"  Gaps observed: {len(gaps)}")
        print(f"  Mean abs gap: {np.mean([g['abs_gap'] for g in gaps]):.0f} pts")
        print(f"  Max abs gap: {gaps[0]['abs_gap']:.0f} pts ({gaps[0]['date']})")

        print("\n  Top 5 gaps:")
        for g in gaps[:5]:
            # Estimate gamma loss for this gap
            idx = next(i for i, d in enumerate(all_days) if d["date"] == g["date"])
            gamma = 2 * bsm_gamma(g["prev_close"], all_days[idx]["atm_strike"],
                                   g["dte"] / 365.0, all_days[idx]["iv"])
            gamma_loss = 0.5 * gamma * g["gap_pts"]**2 * OPT_MULT
            print(f"    {g['date']}: gap={g['gap_pts']:+.0f} pts, "
                  f"gamma_loss={gamma_loss:,.0f} NTD (DTE={g['dte']})")

        # Hypothetical stress: what if gap = 500 pts (limit move)?
        print("\n  Hypothetical stress (limit-down 500 pts):")
        for dte in [5, 10, 20]:
            sample_day = all_days[0]
            gamma = 2 * bsm_gamma(sample_day["open_mid"], sample_day["atm_strike"],
                                   dte / 365.0, sample_day["iv"])
            stress_loss = 0.5 * gamma * 500**2 * OPT_MULT
            print(f"    DTE={dte:>2d}: gamma_loss={stress_loss:>10,.0f} NTD for 500pt gap")

    # ═══════════════════════════════════════════════════════════
    # Step 7: Daily P&L Detail (Best Strategy)
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print("Daily P&L Detail (Smart Hedge)")
    print("=" * 90)

    # Find smart hedge results
    smart_daily = []
    for sr in all_series_results:
        if sr["mode"] == "smart":
            smart_daily.extend(sr["daily_pnls"])
    smart_daily.sort(key=lambda x: x["date"])

    if smart_daily:
        print(f"\n  {'Date':>12s} {'DTE':>4s} {'OptMtM':>9s} {'Theta':>9s} "
              f"{'Gamma':>9s} {'O/N':>9s} {'HdgMtM':>9s} {'HdgSlip':>9s} "
              f"{'Net':>10s} {'Gap':>8s}")
        print("  " + "-" * 100)

        cum = 0.0
        for d in smart_daily:
            cum += d["net_ntd"]
            print(f"  {d['date']:>12s} {d['dte']:>4d} "
                  f"{d['opt_mtm_ntd']:>+9,.0f} {d['theta_ntd']:>+9,.0f} "
                  f"{d['gamma_ntd']:>+9,.0f} {d['overnight_ntd']:>+9,.0f} "
                  f"{d['hedge_mtm_ntd']:>+9,.0f} {d['hedge_slip_ntd']:>9,.0f} "
                  f"{d['net_ntd']:>+10,.0f} {d['gap']:>+8.0f}")
        print(f"  {'TOTAL':>12s} {'':>4s} "
              f"{'':>9s} {'':>9s} {'':>9s} {'':>9s} {'':>9s} {'':>9s} {cum:>+10,.0f}")

    # ═══════════════════════════════════════════════════════════
    # Verdict
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print("VERDICT")
    print("=" * 90)

    if smart_daily:
        nets = np.array([d["net_ntd"] for d in smart_daily])
        n = len(nets)
        split = max(n - HOLDOUT_DAYS, 1)
        train = nets[:split]
        holdout = nets[split:]

        holdout_positive = holdout.mean() > 0 if len(holdout) > 0 else False
        train_positive = train.mean() > 0 if len(train) > 0 else False

        print(f"  Train mean:   {train.mean():>+10,.0f} NTD/day ({len(train)} days)")
        print(f"  Holdout mean: {holdout.mean():>+10,.0f} NTD/day ({len(holdout)} days)")
        print(f"  Win rate:     {sum(nets > 0)/n:.0%}")
        print(f"  Max daily DD: {min(nets):>+10,.0f} NTD")

        if len(nets) >= 2 and nets.std() > 0:
            sharpe = nets.mean() / nets.std() * math.sqrt(252)
            print(f"  Sharpe:       {sharpe:.2f}")

        if train_positive and holdout_positive:
            print(f"\n  >>> PROMISING — Proceed to Stage 2 prototype")
        elif train_positive:
            print(f"\n  >>> MIXED — Train positive but holdout questionable")
        else:
            print(f"\n  >>> FAIL — Strategy not viable")

    print("\n  CAVEATS:")
    print("  1. Only 12 trading days × 2 series — very small sample")
    print("  2. IV approximation (Brenner-Subrahmanyam) vs real market IV")
    print("  3. Constant IV assumption (no smile/skew dynamics)")
    print("  4. Overnight gap modeled as unhedged gamma only (no vega risk)")
    print("  5. No margin requirement or capital constraint modeled")
    print("  6. TXO option liquidity varies significantly by strike/DTE")


if __name__ == "__main__":
    main()
