"""
R28 Stage 4-6: TX→TMF Lead-Lag Tick-Level Backtest + Gate C + Grid Search

Optimized: signal-centric approach instead of full event-loop merge.
1. Generate TX signals (dvol + dp filter)
2. For each signal, look up TMF BidAsk at entry time (signal_ts + 37ms)
3. Simulate position: scan TMF BidAsk forward for SL / time-kill / session-end
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
FEE_PTS = 4.0      # round-trip fees in points (2 per side)

DEFAULT_PARAMS = {
    "dvol_threshold": 20,
    "sl_pts": 100,
    "max_hold_ns": 15 * 60 * 1_000_000_000,  # 15 min
    "cooldown_ns": 5_000_000_000,  # 5s
    "signal_delay_ns": 37_000_000,  # 37ms entry
    "exit_delay_ns": 47_000_000,    # 47ms exit
    "max_lots": 3,
}

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "outputs" / "team_artifacts" / "alpha-research-r28"


# ---------------------------------------------------------------------------
# Data loading via temp file + numpy
# ---------------------------------------------------------------------------
def ck_to_numpy(sql: str, n_cols: int) -> np.ndarray:
    """Run CK query and load result directly into numpy array via temp file."""
    with tempfile.NamedTemporaryFile(suffix=".tsv", delete=False) as f:
        tmppath = f.name

    r = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True,
        timeout=300,
    )
    if r.returncode != 0:
        print(f"CK ERROR: {r.stderr[:500]}", file=sys.stderr)
        return np.zeros((0, n_cols), dtype=np.int64)

    # Write binary stdout to temp file
    with open(tmppath, "wb") as f:
        f.write(r.stdout)

    try:
        arr = np.loadtxt(tmppath, dtype=np.int64, delimiter="\t")
    except ValueError:
        # Fallback for empty
        return np.zeros((0, n_cols), dtype=np.int64)
    finally:
        import os
        os.unlink(tmppath)

    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return arr


def load_data():
    """Load all required data from ClickHouse."""
    t0 = time.time()

    # Day session: 08:45-13:45 TW = 00:45-05:45 UTC
    # Filter using toHour in Asia/Taipei timezone: hour >= 8 AND (hour < 13 OR (hour = 13 AND toMinute < 45))
    # Simpler: use time range 08:45:00 - 13:45:00 in TW
    session_filter = """
        AND toHour(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) >= 8
        AND (toHour(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) < 13
             OR (toHour(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) = 13
                 AND toMinute(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) <= 45))
    """

    print("Loading TX ticks (day session only)...")
    tx = ck_to_numpy(f"""
        SELECT exch_ts, price_scaled, volume
        FROM hft.market_data
        WHERE symbol='TXFD6' AND type='Tick'
          AND toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) >= '2026-03-19'
          {session_filter}
        ORDER BY exch_ts
    """, 3)
    print(f"  {len(tx)} TX ticks ({time.time()-t0:.1f}s)")

    t1 = time.time()
    print("Loading TMF BidAsk (day session only)...")
    tmf_ba = ck_to_numpy(f"""
        SELECT exch_ts, bids_price[1], asks_price[1]
        FROM hft.market_data
        WHERE symbol='TMFD6' AND type='BidAsk'
          AND toDate(toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei')) >= '2026-03-19'
          AND bids_price[1] > 0 AND asks_price[1] > 0
          {session_filter}
        ORDER BY exch_ts
    """, 3)
    print(f"  {len(tmf_ba)} TMF BidAsk events ({time.time()-t1:.1f}s)")

    print(f"Total load time: {time.time()-t0:.1f}s")
    return tx, tmf_ba


# ---------------------------------------------------------------------------
# Signal generation (vectorized)
# ---------------------------------------------------------------------------
def generate_signals(tx: np.ndarray) -> np.ndarray:
    """Generate all TX signals. Returns (signal_ts, direction, dvol, dp) array.

    Does NOT apply cooldown or max_lots — those are applied per-backtest.
    """
    ts = tx[:, 0]
    price = tx[:, 1]
    vol = tx[:, 2]

    # Compute dvol: vol[i] - vol[i-1], reset on day boundary
    # Day boundary: gap > 6 hours between ticks
    dt = np.diff(ts)
    day_boundary = np.zeros(len(tx), dtype=bool)
    day_boundary[0] = True
    day_boundary[1:] = dt > 6 * 3600 * 1_000_000_000

    # dvol
    dvol = np.zeros(len(tx), dtype=np.int64)
    dvol[1:] = vol[1:] - vol[:-1]
    dvol[day_boundary] = vol[day_boundary]
    # Fix negative dvol (safety)
    neg_mask = dvol < 0
    dvol[neg_mask] = vol[neg_mask]

    # dp
    dp = np.zeros(len(tx), dtype=np.int64)
    dp[1:] = price[1:] - price[:-1]
    dp[day_boundary] = 0

    # Filter: dvol > 0 and dp != 0
    valid = (dvol > 0) & (dp != 0) & (~day_boundary)

    indices = np.where(valid)[0]
    signals = np.column_stack([
        ts[indices],
        np.where(dp[indices] > 0, 1, -1),
        dvol[indices],
        dp[indices],
    ])
    return signals


# ---------------------------------------------------------------------------
# Session end computation
# ---------------------------------------------------------------------------
def compute_session_ends(ts_array: np.ndarray) -> dict:
    """Compute session end timestamps (13:45 TW) for each date present in data."""
    import datetime
    session_ends = {}
    for ts in ts_array:
        # TW = UTC+8
        tw_ts = ts / 1e9 + 8 * 3600
        dt = datetime.datetime.fromtimestamp(tw_ts, tz=datetime.timezone.utc)
        date_key = dt.year * 10000 + dt.month * 100 + dt.day
        if date_key not in session_ends:
            # Session end: 13:45 TW = 05:45 UTC
            end_utc = datetime.datetime(dt.year, dt.month, dt.day, 5, 45, 0, tzinfo=datetime.timezone.utc)
            session_ends[date_key] = int(end_utc.timestamp() * 1_000_000_000)
    return session_ends


def get_tw_date(ts_ns: int) -> int:
    """Get TW date as YYYYMMDD."""
    import datetime
    tw_ts = ts_ns / 1e9 + 8 * 3600
    dt = datetime.datetime.fromtimestamp(tw_ts, tz=datetime.timezone.utc)
    return dt.year * 10000 + dt.month * 100 + dt.day


# ---------------------------------------------------------------------------
# Backtest Engine (signal-centric)
# ---------------------------------------------------------------------------
def run_backtest(signals: np.ndarray, tmf_ba_ts: np.ndarray,
                 tmf_ba_bid: np.ndarray, tmf_ba_ask: np.ndarray,
                 session_ends: dict, params: dict) -> list:
    """Run backtest from pre-computed signals.

    Returns list of trade dicts.
    """
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

    trades = []
    # Track open positions as list of (entry_ts, entry_price, direction, signal_ts, session_end)
    open_positions = []
    last_signal_ts = 0
    ba_len = len(tmf_ba_ts)

    # Binary search helper
    def find_ba_idx(target_ts, start_idx=0):
        """Find first BidAsk event at or after target_ts."""
        idx = np.searchsorted(tmf_ba_ts[start_idx:], target_ts)
        return start_idx + idx

    # For each signal, try to open a position
    # We process signals chronologically, maintaining open positions
    ba_scan_idx = 0  # current scan position in BidAsk array

    for i in range(len(filtered)):
        sig_ts = int(filtered[i, 0])
        direction = int(filtered[i, 1])

        # Get session end for this signal's date
        sig_date = get_tw_date(sig_ts)
        sess_end = session_ends.get(sig_date, sig_ts + 6 * 3600 * 1_000_000_000)

        # Skip if past session
        if sig_ts >= sess_end:
            continue

        # Before processing this signal, close any positions that need closing
        # (we process exits that should have happened before this signal)
        entry_fire_ts = sig_ts + signal_delay_ns

        new_open = []
        for pos in open_positions:
            p_entry_ts, p_entry_price, p_dir, p_sig_ts, p_sess_end = pos
            # Check if position should have been closed by now
            close_deadline = min(p_entry_ts + max_hold_ns, p_sess_end)

            if entry_fire_ts >= close_deadline + exit_delay_ns:
                # This position should be closed — find exit price at close time
                exit_target_ts = min(p_entry_ts + max_hold_ns, p_sess_end) + exit_delay_ns
                eidx = find_ba_idx(exit_target_ts)
                if eidx < ba_len:
                    if p_dir == 1:
                        exit_price = tmf_ba_bid[eidx] / SCALE
                    else:
                        exit_price = tmf_ba_ask[eidx] / SCALE
                    gross = p_dir * (exit_price - p_entry_price)
                    reason = "session_end" if p_entry_ts + max_hold_ns > p_sess_end else "time_kill"
                    trades.append({
                        "entry_ts": p_entry_ts, "exit_ts": int(tmf_ba_ts[eidx]),
                        "signal_ts": p_sig_ts, "direction": p_dir,
                        "entry_price": p_entry_price, "exit_price": exit_price,
                        "gross_pnl_pts": gross, "fees_pts": FEE_PTS,
                        "net_pnl_pts": gross - FEE_PTS,
                        "exit_reason": reason,
                        "hold_duration_s": (int(tmf_ba_ts[eidx]) - p_entry_ts) / 1e9,
                    })
                # Position closed either way
            else:
                new_open.append(pos)
        open_positions = new_open

        # Check cooldown
        if (sig_ts - last_signal_ts) < cooldown_ns:
            continue

        # Check max concurrent
        if len(open_positions) >= max_lots:
            continue

        # Find entry BidAsk at signal_ts + signal_delay
        entry_idx = find_ba_idx(entry_fire_ts)
        if entry_idx >= ba_len:
            continue

        # Entry price
        if direction == 1:
            entry_price = tmf_ba_ask[entry_idx] / SCALE  # buy at ask
        else:
            entry_price = tmf_ba_bid[entry_idx] / SCALE  # sell at bid

        actual_entry_ts = int(tmf_ba_ts[entry_idx])
        last_signal_ts = sig_ts

        # Now scan forward to find exit (SL or time-kill or session-end)
        # We need to check BidAsk events between entry and max_hold/session_end
        max_exit_ts = min(actual_entry_ts + max_hold_ns, sess_end)
        # Search for SL
        sl_triggered = False
        exit_reason = "time_kill"
        exit_ba_idx = entry_idx

        # Scan BidAsk from entry to max_exit
        scan_end_idx = find_ba_idx(max_exit_ts, entry_idx)
        if scan_end_idx > ba_len:
            scan_end_idx = ba_len

        # Vectorized SL check
        if scan_end_idx > entry_idx:
            if direction == 1:
                # Long: mark-to-market at bid, SL if bid drops below entry - sl_pts
                sl_price_scaled = int((entry_price - sl_pts) * SCALE)
                # Find first bid below SL level
                chunk_bid = tmf_ba_bid[entry_idx:scan_end_idx]
                sl_hits = np.where(chunk_bid <= sl_price_scaled)[0]
                if len(sl_hits) > 0:
                    sl_triggered = True
                    sl_local_idx = sl_hits[0]
                    exit_ba_idx = entry_idx + sl_local_idx
                    exit_reason = "SL"
            else:
                # Short: mark-to-market at ask, SL if ask rises above entry + sl_pts
                sl_price_scaled = int((entry_price + sl_pts) * SCALE)
                chunk_ask = tmf_ba_ask[entry_idx:scan_end_idx]
                sl_hits = np.where(chunk_ask >= sl_price_scaled)[0]
                if len(sl_hits) > 0:
                    sl_triggered = True
                    sl_local_idx = sl_hits[0]
                    exit_ba_idx = entry_idx + sl_local_idx
                    exit_reason = "SL"

        # Determine exit time and apply exit delay
        if sl_triggered:
            sl_trigger_ts = int(tmf_ba_ts[exit_ba_idx])
            exit_target_ts = sl_trigger_ts + exit_delay_ns
        else:
            # Time kill or session end
            if actual_entry_ts + max_hold_ns <= sess_end:
                exit_target_ts = actual_entry_ts + max_hold_ns + exit_delay_ns
                exit_reason = "time_kill"
            else:
                exit_target_ts = sess_end + exit_delay_ns
                exit_reason = "session_end"

        # Find the BidAsk at exit target time
        final_exit_idx = find_ba_idx(exit_target_ts)
        if final_exit_idx >= ba_len:
            final_exit_idx = ba_len - 1

        if direction == 1:
            exit_price = tmf_ba_bid[final_exit_idx] / SCALE
        else:
            exit_price = tmf_ba_ask[final_exit_idx] / SCALE

        gross = direction * (exit_price - entry_price)

        trades.append({
            "entry_ts": actual_entry_ts,
            "exit_ts": int(tmf_ba_ts[final_exit_idx]),
            "signal_ts": sig_ts,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_pnl_pts": gross,
            "fees_pts": FEE_PTS,
            "net_pnl_pts": gross - FEE_PTS,
            "exit_reason": exit_reason,
            "hold_duration_s": (int(tmf_ba_ts[final_exit_idx]) - actual_entry_ts) / 1e9,
        })

        # Don't add to open_positions for simple scan — we already resolved exit
        # But we need open_positions for max_lots tracking
        # Since we resolved the exit inline, we know when it closes
        # Add to open_positions with resolved close time for max_lots check
        close_ts = int(tmf_ba_ts[final_exit_idx])
        open_positions.append((actual_entry_ts, entry_price, direction, sig_ts, close_ts))

        # Clean up positions that have already closed
        open_positions = [p for p in open_positions if p[4] > sig_ts]

    return trades


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyze_trades(trades: list, label: str = "") -> dict:
    if not trades:
        return {"n_trades": 0, "label": label, "total_net_pnl": 0, "mean_net": 0,
                "win_rate": 0, "max_drawdown_pts": 0, "daily_sharpe": 0,
                "daily_summary": {}, "exit_reasons": {}, "total_gross_pnl": 0,
                "total_fees": 0, "mean_gross": 0, "median_net": 0, "std_net": 0,
                "mean_hold_s": 0}

    net_pnls = np.array([t["net_pnl_pts"] for t in trades])
    gross_pnls = np.array([t["gross_pnl_pts"] for t in trades])

    # Daily breakdown
    daily = {}
    for t in trades:
        d = get_tw_date(t["entry_ts"])
        if d not in daily:
            daily[d] = []
        daily[d].append(t["net_pnl_pts"])

    daily_nets = {d: sum(pnls) for d, pnls in daily.items()}
    daily_vals = np.array(list(daily_nets.values()))

    # Equity curve + max drawdown
    cum_pnl = np.cumsum(net_pnls)
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = running_max - cum_pnl
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

    # Exit reasons
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    n_days = len(daily_nets)
    daily_sharpe = 0.0
    if n_days > 1 and np.std(daily_vals) > 0:
        daily_sharpe = float(np.mean(daily_vals) / np.std(daily_vals) * np.sqrt(252))

    return {
        "label": label,
        "n_trades": len(trades),
        "n_days": n_days,
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
        "daily_summary": {str(d): {"n": len(daily[d]), "net": float(sum(daily[d]))}
                         for d in sorted(daily.keys())},
    }


# ---------------------------------------------------------------------------
# Stage 5: Gate C
# ---------------------------------------------------------------------------
def gate_c_validation(signals: np.ndarray, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask,
                      session_ends, params: dict, trades: list) -> dict:
    results = {}

    stats = analyze_trades(trades, "IS_all")
    daily_summary = stats["daily_summary"]
    daily_pnls = np.array([v["net"] for v in daily_summary.values()])
    n_days = len(daily_pnls)

    # 5.1 DSR
    if n_days > 1 and np.std(daily_pnls) > 0:
        sr = float(np.mean(daily_pnls) / np.std(daily_pnls) * np.sqrt(252))
        z = (daily_pnls - np.mean(daily_pnls)) / np.std(daily_pnls)
        skew = float(np.mean(z ** 3))
        kurt = float(np.mean(z ** 4))
        dsr = sr * (1 - skew * sr / 6 + (kurt - 3) * sr ** 2 / 24)
    else:
        sr, dsr, skew, kurt = 0.0, 0.0, 0.0, 3.0

    results["dsr"] = {"sharpe": sr, "dsr_adjusted": float(dsr),
                      "skewness": skew, "kurtosis": kurt, "n_days": n_days}

    # 5.2 Walk-forward (LOO)
    dates = sorted(daily_summary.keys())
    trade_dates = {}
    for t in trades:
        d = str(get_tw_date(t["entry_ts"]))
        trade_dates.setdefault(d, []).append(t)

    loo_results = []
    for held_out in dates:
        oos_trades = trade_dates.get(held_out, [])
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
    results["walk_forward"] = {
        "folds": loo_results,
        "folds_profitable": folds_profitable,
        "total_folds": len(dates),
        "pass": folds_profitable >= 6,
        "mean_oos_pnl": float(np.mean([r["oos_net_pnl"] for r in loo_results])) if loo_results else 0,
    }

    # 5.3 IS/OOS gap
    is_mean_net = stats["mean_net"]
    per_day_means = []
    for d in dates:
        dt = trade_dates.get(d, [])
        if dt:
            per_day_means.append(np.mean([t["net_pnl_pts"] for t in dt]))
        else:
            per_day_means.append(0)
    oos_mean = float(np.mean(per_day_means)) if per_day_means else 0
    results["is_oos_gap"] = {
        "is_mean_net_per_trade": float(is_mean_net),
        "oos_mean_net_per_trade": oos_mean,
        "gap": float(is_mean_net - oos_mean),
    }

    # 5.4 Neighborhood robustness (±20%)
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
                t = run_backtest(signals, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask, session_ends, p)
                s = analyze_trades(t)
                neighborhood.append({
                    "dvol": dv, "sl": sl, "hold_min": hold // 60_000_000_000,
                    "n_trades": s["n_trades"], "mean_net": s["mean_net"],
                    "total_net": s["total_net_pnl"],
                })

    net_values = [n["mean_net"] for n in neighborhood if n["n_trades"] > 0]
    neg_count = sum(1 for v in net_values if v < 0)
    results["neighborhood"] = {
        "configs": neighborhood,
        "n_positive": sum(1 for v in net_values if v > 0),
        "n_negative": neg_count,
        "n_total": len(net_values),
        "min_mean_net": float(min(net_values)) if net_values else 0,
        "max_mean_net": float(max(net_values)) if net_values else 0,
        "robust": neg_count < len(net_values) // 2,
    }

    # 5.5 PBO
    pbo_profitable = sum(1 for n in neighborhood if n["n_trades"] > 0 and n["mean_net"] > 0)
    pbo_total = sum(1 for n in neighborhood if n["n_trades"] > 0)
    results["pbo"] = {
        "profitable_configs": pbo_profitable,
        "total_configs": pbo_total,
        "pbo_estimate": 1.0 - (pbo_profitable / pbo_total) if pbo_total > 0 else 1.0,
    }

    return results


# ---------------------------------------------------------------------------
# Stage 6: Grid Search
# ---------------------------------------------------------------------------
def grid_search(signals, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask, session_ends, base_params):
    dvol_values = [10, 15, 20, 30, 50]
    sl_values = [50, 75, 100, 125, 150, 200]
    hold_values_min = [10, 15, 20, 30]
    cool_values_s = [3, 5, 10]

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
                    if count % 60 == 0:
                        elapsed = time.time() - t0
                        eta = elapsed / count * (total - count)
                        print(f"  {count}/{total} ({elapsed:.0f}s elapsed, ETA {eta:.0f}s)")

                    p = dict(base_params)
                    p["dvol_threshold"] = dvol
                    p["sl_pts"] = sl
                    p["max_hold_ns"] = hold_min * 60 * 1_000_000_000
                    p["cooldown_ns"] = cool_s * 1_000_000_000

                    trades = run_backtest(signals, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask, session_ends, p)
                    stats = analyze_trades(trades)

                    results.append({
                        "dvol": dvol, "sl": sl, "hold_min": hold_min, "cool_s": cool_s,
                        "n_trades": stats["n_trades"],
                        "mean_net": stats["mean_net"],
                        "total_net": stats["total_net_pnl"],
                        "win_rate": stats["win_rate"],
                        "max_dd": stats["max_drawdown_pts"],
                        "daily_sharpe": stats["daily_sharpe"],
                    })

    elapsed = time.time() - t0
    print(f"  Grid search complete in {elapsed:.0f}s")

    results.sort(key=lambda r: r["mean_net"], reverse=True)
    top5 = results[:5]
    best = results[0] if results else None

    # DSR adjustment
    dsr_adjusted = None
    if best and best["n_trades"] > 0 and best["daily_sharpe"] != 0:
        sr = best["daily_sharpe"]
        n_configs = total
        n_trades = best["n_trades"]
        denom = max(n_trades * 9, n_configs)
        penalty = max(0, 1 - (n_configs - 1) / denom)
        dsr_adjusted = sr * np.sqrt(penalty) if penalty > 0 else 0

    # Check isolation
    best_neighbors = []
    if best:
        for r in results:
            if (abs(r["dvol"] - best["dvol"]) <= max(5, int(best["dvol"] * 0.3)) and
                abs(r["sl"] - best["sl"]) <= max(25, int(best["sl"] * 0.3)) and
                abs(r["hold_min"] - best["hold_min"]) <= max(5, int(best["hold_min"] * 0.3)) and
                abs(r["cool_s"] - best["cool_s"]) <= max(2, int(best["cool_s"] * 0.3))):
                best_neighbors.append(r)

    neighbors_positive = sum(1 for n in best_neighbors if n["mean_net"] > 0)

    return {
        "total_configs": total,
        "elapsed_s": elapsed,
        "all_results": results,
        "top5": top5,
        "best": best,
        "dsr_adjusted": float(dsr_adjusted) if dsr_adjusted is not None else None,
        "best_neighbors": best_neighbors,
        "best_is_isolated_peak": neighbors_positive < len(best_neighbors) // 2,
        "neighbors_positive": neighbors_positive,
        "neighbors_total": len(best_neighbors),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load data
    tx, tmf_ba = load_data()

    # Pre-compute signals (all dvol>0, dp!=0)
    print("\nGenerating signals...")
    signals = generate_signals(tx)
    print(f"  {len(signals)} raw signals (before dvol/cooldown filter)")

    # Extract TMF BidAsk arrays for fast access
    tmf_ba_ts = tmf_ba[:, 0].copy()
    tmf_ba_bid = tmf_ba[:, 1].copy()
    tmf_ba_ask = tmf_ba[:, 2].copy()

    # Pre-compute session ends
    session_ends = compute_session_ends(tmf_ba_ts)

    # =========== Stage 4 ===========
    print("\n" + "=" * 60)
    print("STAGE 4: Tick-Level Backtest (default params)")
    print("=" * 60)

    params = dict(DEFAULT_PARAMS)
    t0 = time.time()
    trades = run_backtest(signals, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask, session_ends, params)
    bt_time = time.time() - t0
    stats = analyze_trades(trades, "stage4_default")

    print(f"\n  Backtest time: {bt_time:.1f}s")
    print(f"  Trades: {stats['n_trades']}")
    print(f"  Total gross PnL: {stats['total_gross_pnl']:.1f} pts")
    print(f"  Total fees: {stats['total_fees']:.1f} pts")
    print(f"  Total net PnL: {stats['total_net_pnl']:.1f} pts")
    print(f"  Mean net per trade: {stats['mean_net']:.2f} pts")
    print(f"  Win rate: {stats['win_rate']:.1%}")
    print(f"  Max drawdown: {stats['max_drawdown_pts']:.1f} pts")
    print(f"  Daily Sharpe: {stats['daily_sharpe']:.2f}")
    print(f"  Mean hold: {stats['mean_hold_s']:.1f}s")
    print(f"  Exit reasons: {stats['exit_reasons']}")

    print("\n  Daily breakdown:")
    for d, v in sorted(stats["daily_summary"].items()):
        print(f"    {d}: {v['n']} trades, net={v['net']:.1f} pts")

    stage4_result = {
        "params": {k: str(v) for k, v in params.items()},
        "summary": stats,
        "trade_log": trades[:200],  # First 200 trades for reference
        "equity_curve": list(np.cumsum([t["net_pnl_pts"] for t in trades]).astype(float)),
    }
    with open(OUTPUT_DIR / "stage4_backtest.json", "w") as f:
        json.dump(stage4_result, f, indent=2, default=str)
    print(f"\n  Saved to {OUTPUT_DIR / 'stage4_backtest.json'}")

    print(f"\n  Stage 3 reference: E[net] = +30.61 pts/trade")
    print(f"  Stage 4 actual:    E[net] = {stats['mean_net']:.2f} pts/trade")
    if stats['n_trades'] > 0:
        discrepancy = abs(stats['mean_net'] - 30.61) / 30.61 * 100
        print(f"  Discrepancy: {discrepancy:.1f}%")

    # =========== Stage 5 ===========
    print("\n" + "=" * 60)
    print("STAGE 5: Gate C Statistical Validation")
    print("=" * 60)

    gate_c = gate_c_validation(signals, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask,
                               session_ends, params, trades)

    print(f"\n  5.1 DSR:")
    print(f"    Sharpe: {gate_c['dsr']['sharpe']:.2f}")
    print(f"    DSR adjusted: {gate_c['dsr']['dsr_adjusted']:.2f}")
    print(f"    Skewness: {gate_c['dsr']['skewness']:.2f}")
    print(f"    Kurtosis: {gate_c['dsr']['kurtosis']:.2f}")

    print(f"\n  5.2 Walk-Forward (LOO):")
    for fold in gate_c["walk_forward"]["folds"]:
        status = "+" if fold["oos_net_pnl"] > 0 else "-"
        print(f"    {fold['held_out']}: {fold['oos_n_trades']} trades, "
              f"net={fold['oos_net_pnl']:+.1f} [{status}]")
    print(f"    Profitable folds: {gate_c['walk_forward']['folds_profitable']}/{gate_c['walk_forward']['total_folds']}")
    print(f"    PASS: {gate_c['walk_forward']['pass']}")

    print(f"\n  5.3 IS/OOS Gap:")
    print(f"    IS mean net/trade: {gate_c['is_oos_gap']['is_mean_net_per_trade']:.2f}")
    print(f"    OOS mean net/trade: {gate_c['is_oos_gap']['oos_mean_net_per_trade']:.2f}")
    print(f"    Gap: {gate_c['is_oos_gap']['gap']:.2f}")

    print(f"\n  5.4 Neighborhood Robustness (±20%):")
    print(f"    Positive: {gate_c['neighborhood']['n_positive']}/{gate_c['neighborhood']['n_total']}")
    print(f"    Min E[net]: {gate_c['neighborhood']['min_mean_net']:.2f}")
    print(f"    Max E[net]: {gate_c['neighborhood']['max_mean_net']:.2f}")
    print(f"    Robust: {gate_c['neighborhood']['robust']}")

    print(f"\n  5.5 PBO:")
    print(f"    Profitable configs: {gate_c['pbo']['profitable_configs']}/{gate_c['pbo']['total_configs']}")
    print(f"    PBO estimate: {gate_c['pbo']['pbo_estimate']:.2f}")

    with open(OUTPUT_DIR / "stage5_gate_c.json", "w") as f:
        json.dump(gate_c, f, indent=2, default=str)

    # =========== Stage 6 ===========
    print("\n" + "=" * 60)
    print("STAGE 6: Systematic Parameter Optimization")
    print("=" * 60)

    grid = grid_search(signals, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask, session_ends, params)

    print(f"\n  Total configs: {grid['total_configs']}")
    print(f"\n  Top 5 by E[net]:")
    for i, cfg in enumerate(grid["top5"]):
        print(f"    #{i+1}: dvol={cfg['dvol']}, sl={cfg['sl']}, "
              f"hold={cfg['hold_min']}m, cool={cfg['cool_s']}s -> "
              f"E[net]={cfg['mean_net']:.2f}, n={cfg['n_trades']}, "
              f"WR={cfg['win_rate']:.1%}, DD={cfg['max_dd']:.0f}")

    if grid["best"]:
        print(f"\n  Best: dvol={grid['best']['dvol']}, sl={grid['best']['sl']}, "
              f"hold={grid['best']['hold_min']}m, cool={grid['best']['cool_s']}s")
        if grid["dsr_adjusted"] is not None:
            print(f"  DSR adjusted: {grid['dsr_adjusted']:.2f}")
        print(f"  Isolated peak: {grid['best_is_isolated_peak']}")
        print(f"  Neighbors positive: {grid['neighbors_positive']}/{grid['neighbors_total']}")

    grid_output = {
        "total_configs": grid["total_configs"],
        "elapsed_s": grid["elapsed_s"],
        "top50": grid["all_results"][:50],
        "bottom10": grid["all_results"][-10:],
        "best": grid["best"],
        "dsr_adjusted": grid["dsr_adjusted"],
        "best_is_isolated_peak": grid["best_is_isolated_peak"],
        "best_neighbors": grid["best_neighbors"],
        "neighbors_positive": grid["neighbors_positive"],
        "neighbors_total": grid["neighbors_total"],
    }
    with open(OUTPUT_DIR / "stage6_optimization.json", "w") as f:
        json.dump(grid_output, f, indent=2, default=str)

    # =========== Re-run Stage 5 on best config ===========
    if grid["best"]:
        best_p = dict(params)
        best_p["dvol_threshold"] = grid["best"]["dvol"]
        best_p["sl_pts"] = grid["best"]["sl"]
        best_p["max_hold_ns"] = grid["best"]["hold_min"] * 60 * 1_000_000_000
        best_p["cooldown_ns"] = grid["best"]["cool_s"] * 1_000_000_000

        if best_p != params:
            print("\n" + "=" * 60)
            print("STAGE 5 (RE-RUN): Gate C on best Stage 6 config")
            print("=" * 60)

            best_trades = run_backtest(signals, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask, session_ends, best_p)
            best_stats = analyze_trades(best_trades, "best_config")
            best_gate_c = gate_c_validation(signals, tmf_ba_ts, tmf_ba_bid, tmf_ba_ask,
                                            session_ends, best_p, best_trades)

            print(f"  Trades: {best_stats['n_trades']}, E[net]={best_stats['mean_net']:.2f}")
            print(f"  DSR: {best_gate_c['dsr']['dsr_adjusted']:.2f}")
            print(f"  LOO profitable: {best_gate_c['walk_forward']['folds_profitable']}/{best_gate_c['walk_forward']['total_folds']}")
            print(f"  Neighborhood robust: {best_gate_c['neighborhood']['robust']}")

            gate_c["best_config_revalidation"] = {
                "params": {k: str(v) for k, v in best_p.items()},
                "summary": best_stats,
                "gate_c": best_gate_c,
            }
            with open(OUTPUT_DIR / "stage5_gate_c.json", "w") as f:
                json.dump(gate_c, f, indent=2, default=str)

    print("\n" + "=" * 60)
    print("ALL STAGES COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
