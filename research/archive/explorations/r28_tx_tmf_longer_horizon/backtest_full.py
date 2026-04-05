"""
R28 Full Validation: TX→TMF Lead-Lag Backtest across 3 front-month pairs (36 days)

Bug fixes from previous run:
  - Bug 1: Exit checks on EVERY TMF event (Tick + BidAsk merged timeline)
  - Bug 2: Proper day-session filtering (08:45-13:45 TW) with >= 100 TX tick threshold

Contract pairs:
  TXFB6/TMFB6: 2026-01-26 to 2026-02-23
  TXFC6/TMFC6: 2026-02-25 to 2026-03-18
  TXFD6/TMFD6: 2026-03-19 to 2026-04-01
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCALE = 1_000_000  # price_scaled / SCALE = points
FEE_PTS = 4.0      # round-trip fees in points

DEFAULT_PARAMS = {
    "dvol_threshold": 20,
    "sl_pts": 100,
    "max_hold_ns": 15 * 60 * 1_000_000_000,  # 15 min
    "cooldown_ns": 5_000_000_000,              # 5s
    "signal_delay_ns": 37_000_000,             # 37ms entry
    "exit_delay_ns": 47_000_000,               # 47ms exit
    "max_lots": 3,
}

PAIRS = [
    ("TXFB6", "TMFB6", "2026-01-26", "2026-02-23"),
    ("TXFC6", "TMFC6", "2026-02-25", "2026-03-18"),
    ("TXFD6", "TMFD6", "2026-03-19", "2026-04-01"),
]

OUTPUT_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "outputs" / "team_artifacts" / "alpha-research-r28"
)

SESSION_FILTER = """
    AND toHour(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) >= 8
    AND (toHour(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) < 13
         OR (toHour(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) = 13
             AND toMinute(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) <= 45))
"""


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def ck_to_numpy(sql: str, n_cols: int) -> np.ndarray:
    """Run CK query -> numpy array via temp file."""
    import os
    with tempfile.NamedTemporaryFile(suffix=".tsv", delete=False) as f:
        tmppath = f.name

    r = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True, timeout=300,
    )
    if r.returncode != 0:
        print(f"CK ERROR: {r.stderr[:500]}", file=sys.stderr)
        return np.zeros((0, n_cols), dtype=np.int64)

    with open(tmppath, "wb") as f:
        f.write(r.stdout)

    try:
        arr = np.loadtxt(tmppath, dtype=np.int64, delimiter="\t")
    except ValueError:
        return np.zeros((0, n_cols), dtype=np.int64)
    finally:
        os.unlink(tmppath)

    if arr.ndim == 1 and n_cols > 1:
        arr = arr.reshape(1, -1)
    elif arr.ndim == 1 and n_cols == 1:
        arr = arr.reshape(-1, 1)
    return arr


def load_pair_data(tx_sym: str, tmf_sym: str, date_from: str, date_to: str):
    """Load TX ticks and TMF BidAsk+Tick for one contract pair, day session only."""
    date_filter = (
        f"AND toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) >= '{date_from}'"
        f" AND toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) <= '{date_to}'"
    )

    print(f"\n  Loading {tx_sym} ticks...")
    tx = ck_to_numpy(f"""
        SELECT exch_ts, price_scaled, volume
        FROM hft.market_data
        WHERE symbol='{tx_sym}' AND type='Tick'
          {date_filter} {SESSION_FILTER}
        ORDER BY exch_ts
    """, 3)
    print(f"    {len(tx)} ticks")

    print(f"  Loading {tmf_sym} BidAsk...")
    tmf_ba = ck_to_numpy(f"""
        SELECT exch_ts, bids_price[1], asks_price[1]
        FROM hft.market_data
        WHERE symbol='{tmf_sym}' AND type='BidAsk'
          {date_filter} {SESSION_FILTER}
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
    """, 3)
    print(f"    {len(tmf_ba)} BidAsk events")

    # Bug 1 fix: Also load TMF Tick events for merged exit timeline
    print(f"  Loading {tmf_sym} Ticks (for merged exit checks)...")
    tmf_tick = ck_to_numpy(f"""
        SELECT exch_ts, price_scaled
        FROM hft.market_data
        WHERE symbol='{tmf_sym}' AND type='Tick'
          {date_filter} {SESSION_FILTER}
        ORDER BY exch_ts
    """, 2)
    print(f"    {len(tmf_tick)} Tick events")

    return tx, tmf_ba, tmf_tick


def filter_valid_dates(tx: np.ndarray, min_ticks: int = 100) -> set:
    """Return set of YYYYMMDD dates with >= min_ticks TX ticks in day session."""
    if len(tx) == 0:
        return set()
    dates = {}
    for i in range(len(tx)):
        d = get_tw_date(int(tx[i, 0]))
        dates[d] = dates.get(d, 0) + 1
    return {d for d, n in dates.items() if n >= min_ticks}


def build_merged_exit_timeline(tmf_ba: np.ndarray, tmf_tick: np.ndarray):
    """Build merged TMF timeline with (ts, bid, ask) for every event.

    For BidAsk events: use actual bid/ask.
    For Tick events: interpolate last known bid/ask from BidAsk stream.
    Returns sorted arrays: (ts, bid, ask).
    """
    if len(tmf_ba) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64), np.array([], dtype=np.int64)

    ba_ts = tmf_ba[:, 0]
    ba_bid = tmf_ba[:, 1]
    ba_ask = tmf_ba[:, 2]

    if len(tmf_tick) == 0:
        return ba_ts.copy(), ba_bid.copy(), ba_ask.copy()

    tick_ts = tmf_tick[:, 0]

    # For each tick event, find the last BidAsk before it
    # searchsorted gives insertion point; idx-1 is last BA <= tick_ts
    insert_idx = np.searchsorted(ba_ts, tick_ts, side='right') - 1

    # Only include ticks where we have a prior BidAsk (idx >= 0)
    valid = insert_idx >= 0
    valid_tick_ts = tick_ts[valid]
    valid_insert_idx = insert_idx[valid]

    tick_bid = ba_bid[valid_insert_idx]
    tick_ask = ba_ask[valid_insert_idx]

    # Merge: concatenate and sort by timestamp
    merged_ts = np.concatenate([ba_ts, valid_tick_ts])
    merged_bid = np.concatenate([ba_bid, tick_bid])
    merged_ask = np.concatenate([ba_ask, tick_ask])

    sort_idx = np.argsort(merged_ts, kind='mergesort')
    return merged_ts[sort_idx], merged_bid[sort_idx], merged_ask[sort_idx]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_tw_date(ts_ns: int) -> int:
    """Get TW date as YYYYMMDD."""
    import datetime
    tw_ts = ts_ns / 1e9 + 8 * 3600
    dt = datetime.datetime.fromtimestamp(tw_ts, tz=datetime.timezone.utc)
    return dt.year * 10000 + dt.month * 100 + dt.day


def compute_session_ends(ts_array: np.ndarray) -> dict:
    """Compute session end timestamps (13:45 TW) for each date."""
    import datetime
    session_ends = {}
    for ts in ts_array:
        date_key = get_tw_date(int(ts))
        if date_key not in session_ends:
            tw_ts = int(ts) / 1e9 + 8 * 3600
            dt = datetime.datetime.fromtimestamp(tw_ts, tz=datetime.timezone.utc)
            end_utc = datetime.datetime(
                dt.year, dt.month, dt.day, 5, 45, 0, tzinfo=datetime.timezone.utc
            )
            session_ends[date_key] = int(end_utc.timestamp() * 1_000_000_000)
    return session_ends


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------
def generate_signals(tx: np.ndarray, valid_dates: set) -> np.ndarray:
    """Generate TX signals filtered to valid dates.

    Returns (signal_ts, direction, dvol, dp) array.
    """
    if len(tx) == 0:
        return np.zeros((0, 4), dtype=np.int64)

    ts = tx[:, 0]
    price = tx[:, 1]
    vol = tx[:, 2]

    # Day boundary: gap > 6 hours
    dt = np.diff(ts)
    day_boundary = np.zeros(len(tx), dtype=bool)
    day_boundary[0] = True
    day_boundary[1:] = dt > 6 * 3600 * 1_000_000_000

    # dvol (cumulative volume diff, reset on day boundary)
    dvol = np.zeros(len(tx), dtype=np.int64)
    dvol[1:] = vol[1:] - vol[:-1]
    dvol[day_boundary] = vol[day_boundary]
    neg_mask = dvol < 0
    dvol[neg_mask] = vol[neg_mask]

    # dp
    dp = np.zeros(len(tx), dtype=np.int64)
    dp[1:] = price[1:] - price[:-1]
    dp[day_boundary] = 0

    # Filter: dvol > 0, dp != 0, not day boundary, valid date
    valid_mask = (dvol > 0) & (dp != 0) & (~day_boundary)

    # Date filter
    if valid_dates:
        date_mask = np.zeros(len(tx), dtype=bool)
        for i in range(len(tx)):
            if valid_mask[i]:
                date_mask[i] = get_tw_date(int(ts[i])) in valid_dates
        valid_mask &= date_mask

    indices = np.where(valid_mask)[0]
    if len(indices) == 0:
        return np.zeros((0, 4), dtype=np.int64)

    return np.column_stack([
        ts[indices],
        np.where(dp[indices] > 0, 1, -1),
        dvol[indices],
        dp[indices],
    ])


# ---------------------------------------------------------------------------
# Backtest Engine (with Bug 1 fix: merged exit timeline)
# ---------------------------------------------------------------------------
def run_backtest(signals: np.ndarray, exit_ts: np.ndarray,
                 exit_bid: np.ndarray, exit_ask: np.ndarray,
                 session_ends: dict, params: dict) -> list:
    """Run backtest using merged exit timeline (BidAsk + Tick events).

    Bug 1 fix: exit checks happen on every TMF event, not just LOBStats.
    """
    if len(signals) == 0 or len(exit_ts) == 0:
        return []

    dvol_threshold = params["dvol_threshold"]
    sl_pts = params["sl_pts"]
    max_hold_ns = params["max_hold_ns"]
    cooldown_ns = params["cooldown_ns"]
    signal_delay_ns = params["signal_delay_ns"]
    exit_delay_ns = params["exit_delay_ns"]
    max_lots = params["max_lots"]

    # Filter signals by dvol threshold
    mask = signals[:, 2] >= dvol_threshold
    filtered = signals[mask]
    if len(filtered) == 0:
        return []

    trades = []
    open_positions = []  # (entry_ts, entry_price, direction, signal_ts, resolved_close_ts)
    last_signal_ts = 0
    ev_len = len(exit_ts)

    def find_idx(target_ts, start_idx=0):
        """Find first event at or after target_ts."""
        idx = np.searchsorted(exit_ts[start_idx:], target_ts)
        return start_idx + idx

    for i in range(len(filtered)):
        sig_ts = int(filtered[i, 0])
        direction = int(filtered[i, 1])

        sig_date = get_tw_date(sig_ts)
        sess_end = session_ends.get(sig_date, sig_ts + 6 * 3600 * 1_000_000_000)

        if sig_ts >= sess_end:
            continue

        entry_fire_ts = sig_ts + signal_delay_ns

        # Cleanup closed positions
        open_positions = [p for p in open_positions if p[4] > sig_ts]

        # Cooldown
        if (sig_ts - last_signal_ts) < cooldown_ns:
            continue

        # Max concurrent lots
        if len(open_positions) >= max_lots:
            continue

        # Find entry BidAsk at signal_ts + signal_delay
        entry_idx = find_idx(entry_fire_ts)
        if entry_idx >= ev_len:
            continue

        # Entry price
        if direction == 1:
            entry_price = exit_ask[entry_idx] / SCALE  # buy at ask
        else:
            entry_price = exit_bid[entry_idx] / SCALE  # sell at bid

        actual_entry_ts = int(exit_ts[entry_idx])
        last_signal_ts = sig_ts

        # Scan forward for exit: SL / time-kill / session-end
        max_exit_ts = min(actual_entry_ts + max_hold_ns, sess_end)
        scan_end_idx = find_idx(max_exit_ts, entry_idx)
        if scan_end_idx > ev_len:
            scan_end_idx = ev_len

        sl_triggered = False
        exit_reason = "time_kill"
        exit_ev_idx = entry_idx

        # Vectorized SL check over merged timeline
        if scan_end_idx > entry_idx:
            if direction == 1:
                # Long: SL if bid drops below entry - sl_pts
                sl_price_scaled = int((entry_price - sl_pts) * SCALE)
                chunk_bid = exit_bid[entry_idx:scan_end_idx]
                sl_hits = np.where(chunk_bid <= sl_price_scaled)[0]
                if len(sl_hits) > 0:
                    sl_triggered = True
                    exit_ev_idx = entry_idx + sl_hits[0]
                    exit_reason = "SL"
            else:
                # Short: SL if ask rises above entry + sl_pts
                sl_price_scaled = int((entry_price + sl_pts) * SCALE)
                chunk_ask = exit_ask[entry_idx:scan_end_idx]
                sl_hits = np.where(chunk_ask >= sl_price_scaled)[0]
                if len(sl_hits) > 0:
                    sl_triggered = True
                    exit_ev_idx = entry_idx + sl_hits[0]
                    exit_reason = "SL"

        # Determine exit target time
        if sl_triggered:
            sl_trigger_ts = int(exit_ts[exit_ev_idx])
            exit_target_ts = sl_trigger_ts + exit_delay_ns
        else:
            if actual_entry_ts + max_hold_ns <= sess_end:
                exit_target_ts = actual_entry_ts + max_hold_ns + exit_delay_ns
                exit_reason = "time_kill"
            else:
                exit_target_ts = sess_end + exit_delay_ns
                exit_reason = "session_end"

        # Find the event at exit target time
        final_exit_idx = find_idx(exit_target_ts)
        if final_exit_idx >= ev_len:
            final_exit_idx = ev_len - 1

        # Guard: ensure exit is on the same calendar day as entry.
        # If exit jumped to next day (gap in data), use last event of entry day.
        exit_date = get_tw_date(int(exit_ts[final_exit_idx]))
        if exit_date != sig_date:
            # Binary search for last event on sig_date: find first event of next day, go back 1
            # Use scan_end_idx which was already capped to max_exit_ts
            if scan_end_idx > entry_idx:
                final_exit_idx = scan_end_idx - 1
            else:
                # No exit events on this day after entry — skip trade
                continue

        if direction == 1:
            exit_price = exit_bid[final_exit_idx] / SCALE
        else:
            exit_price = exit_ask[final_exit_idx] / SCALE

        gross = direction * (exit_price - entry_price)

        trades.append({
            "entry_ts": actual_entry_ts,
            "exit_ts": int(exit_ts[final_exit_idx]),
            "signal_ts": sig_ts,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_pnl_pts": gross,
            "fees_pts": FEE_PTS,
            "net_pnl_pts": gross - FEE_PTS,
            "exit_reason": exit_reason,
            "hold_duration_s": (int(exit_ts[final_exit_idx]) - actual_entry_ts) / 1e9,
        })

        close_ts = int(exit_ts[final_exit_idx])
        open_positions.append((actual_entry_ts, entry_price, direction, sig_ts, close_ts))

    return trades


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyze_trades(trades: list, label: str = "") -> dict:
    if not trades:
        return {
            "n_trades": 0, "label": label, "total_net_pnl": 0, "mean_net": 0,
            "win_rate": 0, "max_drawdown_pts": 0, "daily_sharpe": 0,
            "daily_summary": {}, "exit_reasons": {}, "total_gross_pnl": 0,
            "total_fees": 0, "mean_gross": 0, "median_net": 0, "std_net": 0,
            "mean_hold_s": 0, "n_days": 0, "pct_days_profitable": 0,
        }

    net_pnls = np.array([t["net_pnl_pts"] for t in trades])
    gross_pnls = np.array([t["gross_pnl_pts"] for t in trades])

    daily = {}
    for t in trades:
        d = get_tw_date(t["entry_ts"])
        daily.setdefault(d, []).append(t["net_pnl_pts"])

    daily_nets = {d: sum(pnls) for d, pnls in daily.items()}
    daily_vals = np.array(list(daily_nets.values()))

    cum_pnl = np.cumsum(net_pnls)
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = running_max - cum_pnl
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    n_days = len(daily_nets)
    daily_sharpe = 0.0
    if n_days > 1 and np.std(daily_vals) > 0:
        daily_sharpe = float(np.mean(daily_vals) / np.std(daily_vals) * np.sqrt(252))

    profitable_days = int(np.sum(daily_vals > 0))

    return {
        "label": label,
        "n_trades": len(trades),
        "n_days": n_days,
        "profitable_days": profitable_days,
        "pct_days_profitable": profitable_days / n_days if n_days > 0 else 0,
        "total_gross_pnl": float(np.sum(gross_pnls)),
        "total_fees": float(len(trades) * FEE_PTS),
        "total_net_pnl": float(np.sum(net_pnls)),
        "mean_gross": float(np.mean(gross_pnls)),
        "mean_net": float(np.mean(net_pnls)),
        "median_net": float(np.median(net_pnls)),
        "std_net": float(np.std(net_pnls)),
        "win_rate": float(np.mean(net_pnls > 0)),
        "max_drawdown_pts": max_dd,
        "daily_sharpe": daily_sharpe,
        "exit_reasons": reasons,
        "mean_hold_s": float(np.mean([t["hold_duration_s"] for t in trades])),
        "daily_summary": {
            str(d): {"n": len(daily[d]), "net": float(sum(daily[d]))}
            for d in sorted(daily.keys())
        },
    }


# ---------------------------------------------------------------------------
# Gate C Validation (full)
# ---------------------------------------------------------------------------
def gate_c_full(all_trades: list, pair_trades: dict, signals_by_pair: dict,
                exit_data_by_pair: dict, session_ends_by_pair: dict,
                params: dict) -> dict:
    """Full Gate C across 36 days with all required checks."""
    results = {}

    stats = analyze_trades(all_trades, "full_36d")
    daily_summary = stats["daily_summary"]
    daily_pnls = np.array([v["net"] for v in daily_summary.values()])
    n_days = len(daily_pnls)

    # ----- 5.1 Walk-Forward (Leave-One-Day-Out) -----
    trade_by_date = {}
    for t in all_trades:
        d = str(get_tw_date(t["entry_ts"]))
        trade_by_date.setdefault(d, []).append(t)

    dates = sorted(daily_summary.keys())
    loo_results = []
    for held_out in dates:
        oos_trades = trade_by_date.get(held_out, [])
        oos_pnl = sum(t["net_pnl_pts"] for t in oos_trades)
        oos_n = len(oos_trades)
        oos_wr = float(np.mean([t["net_pnl_pts"] > 0 for t in oos_trades])) if oos_trades else 0
        loo_results.append({
            "held_out": held_out,
            "oos_n_trades": oos_n,
            "oos_net_pnl": float(oos_pnl),
            "oos_win_rate": oos_wr,
        })

    folds_profitable = sum(1 for r in loo_results if r["oos_net_pnl"] > 0)
    target_pct = 0.60
    loo_pass = folds_profitable >= int(n_days * target_pct)

    results["walk_forward"] = {
        "folds": loo_results,
        "folds_profitable": folds_profitable,
        "total_folds": n_days,
        "target_pct": target_pct,
        "pass": loo_pass,
    }

    # ----- 5.2 DSR (Deflated Sharpe) -----
    if n_days > 1 and np.std(daily_pnls) > 0:
        sr = float(np.mean(daily_pnls) / np.std(daily_pnls) * np.sqrt(252))
        z = (daily_pnls - np.mean(daily_pnls)) / np.std(daily_pnls)
        skew = float(np.mean(z ** 3))
        kurt = float(np.mean(z ** 4))
        dsr = sr * (1 - skew * sr / 6 + (kurt - 3) * sr ** 2 / 24)
    else:
        sr, dsr, skew, kurt = 0.0, 0.0, 0.0, 3.0

    results["dsr"] = {
        "sharpe": sr,
        "dsr_adjusted": float(dsr),
        "skewness": skew,
        "kurtosis": kurt,
        "n_days": n_days,
        "pass": dsr > 0,
    }

    # ----- 5.3 Monthly Consistency -----
    monthly = {}
    for pair_label, ptrades in pair_trades.items():
        pstats = analyze_trades(ptrades, pair_label)
        monthly[pair_label] = {
            "n_trades": pstats["n_trades"],
            "n_days": pstats["n_days"],
            "total_net_pnl": pstats["total_net_pnl"],
            "mean_net": pstats["mean_net"],
            "win_rate": pstats["win_rate"],
            "daily_sharpe": pstats["daily_sharpe"],
        }

    # Check consistency: are all months profitable?
    months_profitable = sum(1 for m in monthly.values() if m["total_net_pnl"] > 0)
    results["monthly_consistency"] = {
        "months": monthly,
        "months_profitable": months_profitable,
        "total_months": len(monthly),
        "all_profitable": months_profitable == len(monthly),
    }

    # ----- 5.4 Parameter Neighborhood (+-20%) -----
    base_dvol = params["dvol_threshold"]
    base_sl = params["sl_pts"]
    base_hold = params["max_hold_ns"]

    dvol_neighbors = [max(1, int(base_dvol * 0.8)), base_dvol, int(base_dvol * 1.2)]
    sl_neighbors = [max(1, int(base_sl * 0.8)), base_sl, int(base_sl * 1.2)]
    hold_neighbors = [int(base_hold * 0.8), base_hold, int(base_hold * 1.2)]

    neighborhood = []
    for dv in dvol_neighbors:
        for sl in sl_neighbors:
            for hold in hold_neighbors:
                p = dict(params)
                p["dvol_threshold"] = dv
                p["sl_pts"] = sl
                p["max_hold_ns"] = hold
                # Run across all pairs
                combo_trades = []
                for pair_label in pair_trades:
                    sig = signals_by_pair[pair_label]
                    ets, ebid, eask = exit_data_by_pair[pair_label]
                    se = session_ends_by_pair[pair_label]
                    combo_trades.extend(run_backtest(sig, ets, ebid, eask, se, p))
                s = analyze_trades(combo_trades)
                neighborhood.append({
                    "dvol": dv, "sl": sl, "hold_min": hold // 60_000_000_000,
                    "n_trades": s["n_trades"], "mean_net": s["mean_net"],
                    "total_net": s["total_net_pnl"],
                })

    net_values = [n["mean_net"] for n in neighborhood if n["n_trades"] > 0]
    n_positive = sum(1 for v in net_values if v > 0)
    results["neighborhood"] = {
        "configs": neighborhood,
        "n_positive": n_positive,
        "n_negative": len(net_values) - n_positive,
        "n_total": len(net_values),
        "min_mean_net": float(min(net_values)) if net_values else 0,
        "max_mean_net": float(max(net_values)) if net_values else 0,
        "majority_positive": n_positive > len(net_values) // 2,
    }

    # ----- 5.5 Bootstrap CI (day-clustered) -----
    if n_days >= 5:
        rng = np.random.default_rng(42)
        n_boot = 10000
        boot_means = np.zeros(n_boot)
        for b in range(n_boot):
            sample_idx = rng.choice(n_days, size=n_days, replace=True)
            boot_means[b] = daily_pnls[sample_idx].mean()
        ci_lower = float(np.percentile(boot_means, 2.5))
        ci_upper = float(np.percentile(boot_means, 97.5))
        excludes_zero = ci_lower > 0 or ci_upper < 0
    else:
        ci_lower, ci_upper, excludes_zero = 0, 0, False

    results["bootstrap_ci"] = {
        "ci_95_lower": ci_lower,
        "ci_95_upper": ci_upper,
        "excludes_zero": excludes_zero,
        "n_boot": 10000,
        "pass": excludes_zero,
    }

    # ----- Overall Gate C verdict -----
    results["verdict"] = {
        "walk_forward_pass": loo_pass,
        "dsr_pass": dsr > 0,
        "monthly_consistent": months_profitable == len(monthly),
        "neighborhood_robust": n_positive > len(net_values) // 2,
        "bootstrap_pass": excludes_zero,
        "GATE_C_PASS": (
            loo_pass
            and dsr > 0
            and n_positive > len(net_values) // 2
            and excludes_zero
        ),
    }

    return results


# ---------------------------------------------------------------------------
# Grid Search (Stage 6)
# ---------------------------------------------------------------------------
def grid_search(signals_by_pair, exit_data_by_pair, session_ends_by_pair, base_params):
    dvol_values = [10, 15, 20, 30]
    sl_values = [75, 100, 150, 200]
    hold_values_min = [10, 15, 20, 30]
    cool_values_s = [5, 10]

    total = len(dvol_values) * len(sl_values) * len(hold_values_min) * len(cool_values_s)
    print(f"\nStage 6: Grid search over {total} configurations...")

    results = []
    count = 0
    t0 = time.time()

    for dvol in dvol_values:
        for sl in sl_values:
            for hold_min in hold_values_min:
                for cool_s in cool_values_s:
                    count += 1
                    if count % 32 == 0:
                        elapsed = time.time() - t0
                        eta = elapsed / count * (total - count)
                        print(f"  {count}/{total} ({elapsed:.0f}s elapsed, ETA {eta:.0f}s)")

                    p = dict(base_params)
                    p["dvol_threshold"] = dvol
                    p["sl_pts"] = sl
                    p["max_hold_ns"] = hold_min * 60 * 1_000_000_000
                    p["cooldown_ns"] = cool_s * 1_000_000_000

                    combo_trades = []
                    for pair_label in signals_by_pair:
                        sig = signals_by_pair[pair_label]
                        ets, ebid, eask = exit_data_by_pair[pair_label]
                        se = session_ends_by_pair[pair_label]
                        combo_trades.extend(run_backtest(sig, ets, ebid, eask, se, p))

                    s = analyze_trades(combo_trades)

                    results.append({
                        "dvol": dvol, "sl": sl, "hold_min": hold_min, "cool_s": cool_s,
                        "n_trades": s["n_trades"],
                        "mean_net": s["mean_net"],
                        "total_net": s["total_net_pnl"],
                        "win_rate": s["win_rate"],
                        "max_dd": s["max_drawdown_pts"],
                        "daily_sharpe": s["daily_sharpe"],
                        "n_days": s["n_days"],
                        "pct_profitable_days": s["pct_days_profitable"],
                    })

    elapsed = time.time() - t0
    print(f"  Grid search complete in {elapsed:.0f}s")

    results.sort(key=lambda r: r["mean_net"], reverse=True)
    top10 = results[:10]
    best = results[0] if results else None

    return {
        "total_configs": total,
        "elapsed_s": elapsed,
        "all_results": results,
        "top10": top10,
        "best": best,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    # ===== Load data for all 3 pairs =====
    print("=" * 70)
    print("R28 FULL VALIDATION: 3 front-month pairs, ~36 trading days")
    print("=" * 70)

    signals_by_pair = {}
    exit_data_by_pair = {}
    session_ends_by_pair = {}
    pair_labels = []
    all_valid_dates = {}

    for tx_sym, tmf_sym, date_from, date_to in PAIRS:
        pair_label = f"{tx_sym}/{tmf_sym}"
        pair_labels.append(pair_label)
        print(f"\n--- Pair: {pair_label} ({date_from} to {date_to}) ---")

        tx, tmf_ba, tmf_tick = load_pair_data(tx_sym, tmf_sym, date_from, date_to)

        # Bug 2 fix: filter to valid dates (>= 100 TX ticks in day session)
        valid_dates = filter_valid_dates(tx, min_ticks=100)
        all_valid_dates[pair_label] = valid_dates
        print(f"  Valid day-session dates: {len(valid_dates)} "
              f"({sorted(valid_dates)})")

        if len(tx) == 0 or len(tmf_ba) == 0:
            print(f"  SKIPPING: insufficient data")
            signals_by_pair[pair_label] = np.zeros((0, 4), dtype=np.int64)
            exit_data_by_pair[pair_label] = (
                np.array([], dtype=np.int64),
                np.array([], dtype=np.int64),
                np.array([], dtype=np.int64),
            )
            session_ends_by_pair[pair_label] = {}
            continue

        # Generate signals
        signals = generate_signals(tx, valid_dates)
        signals_by_pair[pair_label] = signals
        print(f"  Signals (dvol>0, dp!=0): {len(signals)}")

        # Build merged exit timeline (Bug 1 fix)
        merged_ts, merged_bid, merged_ask = build_merged_exit_timeline(tmf_ba, tmf_tick)
        exit_data_by_pair[pair_label] = (merged_ts, merged_bid, merged_ask)
        print(f"  Merged exit timeline: {len(merged_ts)} events "
              f"(BidAsk={len(tmf_ba)}, Tick={len(tmf_tick)})")

        # Session ends
        session_ends = compute_session_ends(merged_ts)
        # Filter session ends to valid dates only
        session_ends = {d: ts for d, ts in session_ends.items() if d in valid_dates}
        session_ends_by_pair[pair_label] = session_ends

    load_time = time.time() - t_start
    print(f"\nTotal data load time: {load_time:.1f}s")

    total_valid = sum(len(v) for v in all_valid_dates.values())
    print(f"Total valid trading days: {total_valid}")

    # ===== STAGE 4: Backtest with default params =====
    print("\n" + "=" * 70)
    print("STAGE 4: Tick-Level Backtest (default params, all 3 pairs)")
    print("=" * 70)

    params = dict(DEFAULT_PARAMS)
    all_trades = []
    pair_trades = {}

    for pair_label in pair_labels:
        sig = signals_by_pair[pair_label]
        ets, ebid, eask = exit_data_by_pair[pair_label]
        se = session_ends_by_pair[pair_label]

        t0 = time.time()
        trades = run_backtest(sig, ets, ebid, eask, se, params)
        bt_time = time.time() - t0

        pair_trades[pair_label] = trades
        all_trades.extend(trades)

        pstats = analyze_trades(trades, pair_label)
        print(f"\n  {pair_label}: {pstats['n_trades']} trades over {pstats['n_days']} days "
              f"({bt_time:.1f}s)")
        print(f"    Total net: {pstats['total_net_pnl']:.1f} pts, "
              f"E[net]: {pstats['mean_net']:.2f} pts/trade, "
              f"WR: {pstats['win_rate']:.1%}")
        print(f"    Daily Sharpe: {pstats['daily_sharpe']:.2f}, "
              f"Max DD: {pstats['max_drawdown_pts']:.1f} pts")
        print(f"    Exit reasons: {pstats['exit_reasons']}")
        print(f"    Days profitable: {pstats['profitable_days']}/{pstats['n_days']} "
              f"({pstats['pct_days_profitable']:.0%})")
        if pstats["daily_summary"]:
            print(f"    Daily breakdown:")
            for d, v in sorted(pstats["daily_summary"].items()):
                print(f"      {d}: {v['n']} trades, net={v['net']:+.1f} pts")

    # Combined stats
    combined_stats = analyze_trades(all_trades, "combined_36d")
    print(f"\n  COMBINED: {combined_stats['n_trades']} trades over "
          f"{combined_stats['n_days']} days")
    print(f"    Total net: {combined_stats['total_net_pnl']:.1f} pts, "
          f"E[net]: {combined_stats['mean_net']:.2f} pts/trade")
    print(f"    Win rate: {combined_stats['win_rate']:.1%}")
    print(f"    Daily Sharpe: {combined_stats['daily_sharpe']:.2f}, "
          f"Max DD: {combined_stats['max_drawdown_pts']:.1f} pts")
    print(f"    Mean hold: {combined_stats['mean_hold_s']:.1f}s")
    print(f"    Exit reasons: {combined_stats['exit_reasons']}")
    print(f"    Days profitable: {combined_stats['profitable_days']}/"
          f"{combined_stats['n_days']} "
          f"({combined_stats['pct_days_profitable']:.0%})")

    # Verify Bug 1 fix: check for trades held beyond max_hold
    max_hold_s = params["max_hold_ns"] / 1e9
    over_hold = [t for t in all_trades if t["hold_duration_s"] > max_hold_s + 1.0]
    print(f"\n  Bug 1 check: {len(over_hold)} trades held > max_hold+1s "
          f"(should be ~0, was 36/243 before fix)")

    # Save Stage 4
    stage4_result = {
        "params": {k: str(v) for k, v in params.items()},
        "combined_summary": combined_stats,
        "per_pair_summary": {
            pl: analyze_trades(pt, pl) for pl, pt in pair_trades.items()
        },
        "trade_log_sample": all_trades[:300],
        "bug1_over_hold": len(over_hold),
    }
    with open(OUTPUT_DIR / "full_stage4_backtest.json", "w") as f:
        json.dump(stage4_result, f, indent=2, default=str)
    print(f"\n  Saved to {OUTPUT_DIR / 'full_stage4_backtest.json'}")

    # ===== STAGE 5: Gate C =====
    print("\n" + "=" * 70)
    print("STAGE 5: Gate C Statistical Validation (36 days)")
    print("=" * 70)

    gate_c = gate_c_full(
        all_trades, pair_trades, signals_by_pair,
        exit_data_by_pair, session_ends_by_pair, params
    )

    # Print results
    print(f"\n  5.1 Walk-Forward (Leave-One-Day-Out):")
    for fold in gate_c["walk_forward"]["folds"]:
        status = "+" if fold["oos_net_pnl"] > 0 else "-"
        print(f"    {fold['held_out']}: {fold['oos_n_trades']} trades, "
              f"net={fold['oos_net_pnl']:+.1f} [{status}]")
    print(f"    Profitable folds: {gate_c['walk_forward']['folds_profitable']}/"
          f"{gate_c['walk_forward']['total_folds']} "
          f"(target >= {gate_c['walk_forward']['target_pct']:.0%})")
    print(f"    PASS: {gate_c['walk_forward']['pass']}")

    print(f"\n  5.2 DSR:")
    print(f"    Sharpe: {gate_c['dsr']['sharpe']:.2f}")
    print(f"    DSR adjusted: {gate_c['dsr']['dsr_adjusted']:.2f}")
    print(f"    Skewness: {gate_c['dsr']['skewness']:.2f}")
    print(f"    Kurtosis: {gate_c['dsr']['kurtosis']:.2f}")
    print(f"    PASS: {gate_c['dsr']['pass']}")

    print(f"\n  5.3 Monthly Consistency:")
    for month, mdata in gate_c["monthly_consistency"]["months"].items():
        status = "+" if mdata["total_net_pnl"] > 0 else "-"
        print(f"    {month}: {mdata['n_trades']} trades over {mdata['n_days']} days, "
              f"net={mdata['total_net_pnl']:+.1f} pts, "
              f"E[net]={mdata['mean_net']:+.2f}, WR={mdata['win_rate']:.1%} [{status}]")
    print(f"    Months profitable: {gate_c['monthly_consistency']['months_profitable']}/"
          f"{gate_c['monthly_consistency']['total_months']}")
    print(f"    All profitable: {gate_c['monthly_consistency']['all_profitable']}")

    print(f"\n  5.4 Neighborhood Robustness (+-20%):")
    print(f"    Positive: {gate_c['neighborhood']['n_positive']}/"
          f"{gate_c['neighborhood']['n_total']}")
    print(f"    Min E[net]: {gate_c['neighborhood']['min_mean_net']:.2f}")
    print(f"    Max E[net]: {gate_c['neighborhood']['max_mean_net']:.2f}")
    print(f"    Majority positive: {gate_c['neighborhood']['majority_positive']}")

    print(f"\n  5.5 Bootstrap CI (day-clustered, 95%):")
    print(f"    CI: [{gate_c['bootstrap_ci']['ci_95_lower']:.2f}, "
          f"{gate_c['bootstrap_ci']['ci_95_upper']:.2f}]")
    print(f"    Excludes zero: {gate_c['bootstrap_ci']['excludes_zero']}")
    print(f"    PASS: {gate_c['bootstrap_ci']['pass']}")

    print(f"\n  GATE C VERDICT:")
    for k, v in gate_c["verdict"].items():
        status = "PASS" if v else "FAIL"
        print(f"    {k}: {status}")

    with open(OUTPUT_DIR / "full_stage5_gate_c.json", "w") as f:
        json.dump(gate_c, f, indent=2, default=str)
    print(f"\n  Saved to {OUTPUT_DIR / 'full_stage5_gate_c.json'}")

    # ===== STAGE 6: Grid Search =====
    print("\n" + "=" * 70)
    print("STAGE 6: Parameter Optimization (128 configs)")
    print("=" * 70)

    grid = grid_search(signals_by_pair, exit_data_by_pair, session_ends_by_pair, params)

    print(f"\n  Total configs: {grid['total_configs']}")
    print(f"\n  Top 10 by E[net]:")
    for i, cfg in enumerate(grid["top10"]):
        print(f"    #{i+1}: dvol={cfg['dvol']}, sl={cfg['sl']}, "
              f"hold={cfg['hold_min']}m, cool={cfg['cool_s']}s -> "
              f"E[net]={cfg['mean_net']:.2f}, n={cfg['n_trades']}, "
              f"WR={cfg['win_rate']:.1%}, DD={cfg['max_dd']:.0f}, "
              f"Sharpe={cfg['daily_sharpe']:.2f}, "
              f"days_profit={cfg['pct_profitable_days']:.0%}")

    # Save grid
    grid_output = {
        "total_configs": grid["total_configs"],
        "elapsed_s": grid["elapsed_s"],
        "top50": grid["all_results"][:50],
        "bottom10": grid["all_results"][-10:],
        "best": grid["best"],
    }
    with open(OUTPUT_DIR / "full_stage6_optimization.json", "w") as f:
        json.dump(grid_output, f, indent=2, default=str)

    # ===== Re-run Gate C on best config if different =====
    best_gate_c = None
    if grid["best"]:
        best_p = dict(params)
        best_p["dvol_threshold"] = grid["best"]["dvol"]
        best_p["sl_pts"] = grid["best"]["sl"]
        best_p["max_hold_ns"] = grid["best"]["hold_min"] * 60 * 1_000_000_000
        best_p["cooldown_ns"] = grid["best"]["cool_s"] * 1_000_000_000

        is_different = (
            best_p["dvol_threshold"] != params["dvol_threshold"]
            or best_p["sl_pts"] != params["sl_pts"]
            or best_p["max_hold_ns"] != params["max_hold_ns"]
            or best_p["cooldown_ns"] != params["cooldown_ns"]
        )

        if is_different:
            print("\n" + "=" * 70)
            print("STAGE 5 RE-RUN: Gate C on best Stage 6 config")
            print(f"  Best: dvol={grid['best']['dvol']}, sl={grid['best']['sl']}, "
                  f"hold={grid['best']['hold_min']}m, cool={grid['best']['cool_s']}s")
            print("=" * 70)

            best_all_trades = []
            best_pair_trades = {}
            for pair_label in pair_labels:
                sig = signals_by_pair[pair_label]
                ets, ebid, eask = exit_data_by_pair[pair_label]
                se = session_ends_by_pair[pair_label]
                bt = run_backtest(sig, ets, ebid, eask, se, best_p)
                best_pair_trades[pair_label] = bt
                best_all_trades.extend(bt)

            best_stats = analyze_trades(best_all_trades, "best_config")
            print(f"  Trades: {best_stats['n_trades']}, "
                  f"E[net]={best_stats['mean_net']:.2f}, "
                  f"WR={best_stats['win_rate']:.1%}")

            best_gate_c = gate_c_full(
                best_all_trades, best_pair_trades, signals_by_pair,
                exit_data_by_pair, session_ends_by_pair, best_p
            )
            print(f"  DSR: {best_gate_c['dsr']['dsr_adjusted']:.2f}")
            print(f"  LOO profitable: {best_gate_c['walk_forward']['folds_profitable']}/"
                  f"{best_gate_c['walk_forward']['total_folds']}")
            print(f"  Bootstrap CI: [{best_gate_c['bootstrap_ci']['ci_95_lower']:.2f}, "
                  f"{best_gate_c['bootstrap_ci']['ci_95_upper']:.2f}]")
            print(f"  GATE C PASS: {best_gate_c['verdict']['GATE_C_PASS']}")

    # ===== Save combined results =====
    total_time = time.time() - t_start
    final_result = {
        "meta": {
            "total_runtime_s": total_time,
            "pairs": [f"{tx}/{tmf}" for tx, tmf, _, _ in PAIRS],
            "total_valid_days": total_valid,
            "valid_dates_per_pair": {
                pl: sorted(list(vd)) for pl, vd in all_valid_dates.items()
            },
        },
        "stage4_default": {
            "params": {k: str(v) for k, v in params.items()},
            "combined_summary": combined_stats,
            "per_pair_summary": {
                pl: analyze_trades(pt, pl) for pl, pt in pair_trades.items()
            },
        },
        "stage5_gate_c_default": gate_c,
        "stage6_best_config": grid["best"],
        "stage5_gate_c_best": best_gate_c,
    }

    with open(OUTPUT_DIR / "full_validation_results.json", "w") as f:
        json.dump(final_result, f, indent=2, default=str)
    print(f"\nSaved combined results to {OUTPUT_DIR / 'full_validation_results.json'}")

    # ===== Final Summary =====
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(f"  Total runtime: {total_time:.1f}s")
    print(f"  Valid trading days: {total_valid}")
    print(f"  Default params: dvol={params['dvol_threshold']}, sl={params['sl_pts']}, "
          f"hold={params['max_hold_ns']//60_000_000_000}m")
    print(f"  Combined trades: {combined_stats['n_trades']}")
    print(f"  Combined E[net]: {combined_stats['mean_net']:.2f} pts/trade")
    print(f"  Combined Sharpe: {combined_stats['daily_sharpe']:.2f}")
    print(f"  Gate C (default): {'PASS' if gate_c['verdict']['GATE_C_PASS'] else 'FAIL'}")
    if grid["best"]:
        print(f"  Best grid config: dvol={grid['best']['dvol']}, sl={grid['best']['sl']}, "
              f"hold={grid['best']['hold_min']}m, cool={grid['best']['cool_s']}s")
        print(f"  Best E[net]: {grid['best']['mean_net']:.2f}")
    if best_gate_c:
        print(f"  Gate C (best): {'PASS' if best_gate_c['verdict']['GATE_C_PASS'] else 'FAIL'}")


if __name__ == "__main__":
    main()
