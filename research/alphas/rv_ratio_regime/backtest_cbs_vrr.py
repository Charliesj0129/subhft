"""CBS + vrr gating backtest on TMFD6 L1 data.

Compares ungated CBS-40-300 vs vrr-gated CBS across multiple thresholds.
Uses PRODUCTION vrr formula (raw difference, exact alphas).

Usage:
    python -m research.alphas.rv_ratio_regime.backtest_cbs_vrr
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parents[3] / "research" / "data" / "raw" / "tmfd6"

# Production VRR constants (must match engine.py exactly)
_VRR_ALPHA_SHORT: float = 0.017179401454748944  # 1 - exp(-ln(2) * 0.125 / 5.0)
_VRR_ALPHA_LONG: float = 0.00028876962325730116  # 1 - exp(-ln(2) * 0.125 / 300.0)
_VRR_WARMUP: int = 2400
_VRR_CLAMP_MAX: float = 10.0

# CBS-40-300 config (R14)
CBS_MOVE_BPS: float = 40.0
CBS_LOOKBACK_S: float = 600.0
CBS_HOLD_S: float = 300.0
CBS_STOP_BPS: float = 15.0
CBS_COOLDOWN_S: float = 10.0

# TMFD6 cost
TMFD6_RT_COST_PTS: float = 3.92  # tax 6.6 + comm 13 per side, 1 pt = 10 NTD

# Latency: 36ms P95 submit
LATENCY_NS: int = 36_000_000


def load_all_days() -> list[tuple[str, np.ndarray]]:
    files = sorted(DATA_DIR.glob("TMFD6_2026-*_l1.npy"))
    days = []
    for f in files:
        date_str = f.stem.split("_")[1]
        arr = np.load(f, allow_pickle=True)
        if len(arr) < 100:
            continue
        days.append((date_str, arr))
    return days


def compute_vrr_production(mid: np.ndarray) -> np.ndarray:
    """Compute vrr using PRODUCTION formula (raw difference, exact alphas).

    Matches engine.py _compute_vrr() exactly.
    """
    n = len(mid)
    vrr = np.full(n, np.nan)

    a_s = _VRR_ALPHA_SHORT
    a_l = _VRR_ALPHA_LONG

    ew_mean_s = 0.0
    ew_var_s = 0.0
    ew_mean_l = 0.0
    ew_var_l = 0.0

    for i in range(1, n):
        ret = float(mid[i]) - float(mid[i - 1])

        delta_s = ret - ew_mean_s
        ew_mean_s += a_s * delta_s
        ew_var_s = (1.0 - a_s) * (ew_var_s + a_s * delta_s * delta_s)

        delta_l = ret - ew_mean_l
        ew_mean_l += a_l * delta_l
        ew_var_l = (1.0 - a_l) * (ew_var_l + a_l * delta_l * delta_l)

        if i >= _VRR_WARMUP and ew_var_l > 1e-15:
            v = ew_var_s / ew_var_l
            vrr[i] = max(0.0, min(_VRR_CLAMP_MAX, v))

    return vrr


def run_cbs_backtest(
    mid: np.ndarray,
    ts_ns: np.ndarray,
    vrr: np.ndarray,
    vrr_threshold: float | None,
) -> list[dict]:
    """Run CBS-40-300 backtest with optional vrr gating.

    vrr_threshold=None → ungated (baseline).
    vrr_threshold=X → only enter when vrr < X.

    Applies latency: entry at tick i+latency_ticks, exit same.
    Cost deducted from P&L.
    """
    n = len(mid)
    lookback_ns = int(CBS_LOOKBACK_S * 1e9)
    hold_ns = int(CBS_HOLD_S * 1e9)
    cooldown_ns = int(CBS_COOLDOWN_S * 1e9)
    trades = []
    prev_ptr = 0
    next_allowed_ns = 0

    for i in range(1, n):
        if ts_ns[i] < next_allowed_ns:
            continue

        # Find price at lookback
        while prev_ptr < i and ts_ns[i] - ts_ns[prev_ptr] > lookback_ns:
            prev_ptr += 1
        if prev_ptr >= i:
            continue

        move = float(mid[i]) - float(mid[prev_ptr])
        mid_val = float(mid[i])
        if mid_val <= 0:
            continue
        move_bp = abs(move) / mid_val * 10000.0
        if move_bp < CBS_MOVE_BPS:
            continue

        # vrr gate check (BEFORE entry, no lookahead)
        if vrr_threshold is not None:
            if not np.isfinite(vrr[i]) or vrr[i] >= vrr_threshold:
                continue

        # Apply latency: find entry tick after latency
        entry_idx = i
        entry_ts = ts_ns[i] + LATENCY_NS
        while entry_idx < n - 1 and ts_ns[entry_idx] < entry_ts:
            entry_idx += 1
        if entry_idx >= n - 1:
            continue

        direction = -1.0 if move > 0 else 1.0  # contrarian
        entry_price = float(mid[entry_idx])

        # Find exit: hold time or stop loss
        exit_idx = None
        exit_reason = "timeout"
        hold_deadline = ts_ns[entry_idx] + hold_ns

        for j in range(entry_idx + 1, n):
            # Stop loss check
            unrealized = direction * (float(mid[j]) - entry_price)
            unrealized_bps = abs(unrealized) / entry_price * 10000.0 if entry_price > 0 else 0.0
            if unrealized < 0 and unrealized_bps > CBS_STOP_BPS:
                exit_idx = j
                exit_reason = "stop"
                break
            # Hold timeout
            if ts_ns[j] >= hold_deadline:
                exit_idx = j
                exit_reason = "hold"
                break

        if exit_idx is None:
            continue

        # Apply latency to exit
        exit_ts = ts_ns[exit_idx] + LATENCY_NS
        actual_exit = exit_idx
        while actual_exit < n - 1 and ts_ns[actual_exit] < exit_ts:
            actual_exit += 1

        exit_price = float(mid[actual_exit])
        raw_pnl = direction * (exit_price - entry_price)

        # Convert to points (mid_price is already x10000 scale in the data,
        # but for TMFD6 the price IS in points directly based on the data format)
        # Actually mid_price in data is float with 1pt precision
        pnl_pts = raw_pnl
        pnl_net_pts = pnl_pts - TMFD6_RT_COST_PTS

        trades.append({
            "entry_ts": ts_ns[entry_idx],
            "exit_ts": ts_ns[actual_exit],
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pts": pnl_pts,
            "pnl_net_pts": pnl_net_pts,
            "vrr_at_signal": float(vrr[i]) if np.isfinite(vrr[i]) else np.nan,
            "exit_reason": exit_reason,
        })

        next_allowed_ns = ts_ns[actual_exit] + cooldown_ns

    return trades


def compute_metrics(trades: list[dict], label: str) -> dict:
    """Compute backtest metrics from trade list."""
    if not trades:
        return {"label": label, "n_trades": 0}

    pnl = np.array([t["pnl_net_pts"] for t in trades])
    n = len(pnl)
    mean_pnl = float(np.mean(pnl))
    # Convert to bps for mean_pnl_bps (using avg entry price)
    avg_price = np.mean([t["entry_price"] for t in trades])
    mean_pnl_bps = mean_pnl / avg_price * 10000.0 if avg_price > 0 else 0.0

    wins = int(np.sum(pnl > 0))
    win_rate = wins / n * 100.0

    # Max drawdown in points
    cumsum = np.cumsum(pnl)
    peak = np.maximum.accumulate(cumsum)
    dd = peak - cumsum
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

    # Annualized Sharpe (assume ~5 trades/day, 252 trading days)
    if np.std(pnl) > 0:
        daily_trades = 5.0  # rough estimate
        sharpe = float(np.mean(pnl) / np.std(pnl) * math.sqrt(daily_trades * 252))
    else:
        sharpe = 0.0

    return {
        "label": label,
        "n_trades": n,
        "mean_pnl_pts": mean_pnl,
        "mean_pnl_bps": mean_pnl_bps,
        "win_rate": win_rate,
        "max_dd_pts": max_dd,
        "sharpe_ann": sharpe,
    }


def main() -> None:
    print("=" * 80, flush=True)
    print("CBS + vrr Gating Backtest — TMFD6", flush=True)
    print("=" * 80, flush=True)

    days = load_all_days()
    if not days:
        print("ERROR: No data")
        return

    dates = [d for d, _ in days]
    print(f"Loaded {len(days)} days: {dates[0]} to {dates[-1]}", flush=True)

    is_days = [(d, arr) for d, arr in days if d < "2026-03"]
    oos_days = [(d, arr) for d, arr in days if d >= "2026-03"]
    print(f"IS: {len(is_days)} days, OOS: {len(oos_days)} days", flush=True)
    print(f"CBS config: {CBS_MOVE_BPS} bps move, {CBS_LOOKBACK_S}s lookback, "
          f"{CBS_HOLD_S}s hold, {CBS_STOP_BPS} bps stop", flush=True)
    print(f"Cost: {TMFD6_RT_COST_PTS} pts RT, Latency: {LATENCY_NS/1e6:.0f}ms", flush=True)

    thresholds = [None, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0]

    # Run backtests
    for split_name, split_days in [("IS", is_days), ("OOS", oos_days)]:
        print(f"\n{'='*60}", flush=True)
        print(f"  {split_name} RESULTS", flush=True)
        print(f"{'='*60}", flush=True)

        # Compute vrr and run CBS for each day, aggregate per threshold
        trades_by_thresh: dict[float | None, list[dict]] = {t: [] for t in thresholds}
        baseline_signals = 0  # total CBS signals (ungated)

        for date_str, arr in split_days:
            mid = arr["mid_price"]
            ts = arr["local_ts"]
            vrr = compute_vrr_production(mid)

            for thresh in thresholds:
                day_trades = run_cbs_backtest(mid, ts, vrr, thresh)
                for t in day_trades:
                    t["date"] = date_str
                trades_by_thresh[thresh].extend(day_trades)

            if None in trades_by_thresh:
                baseline_signals = len(trades_by_thresh[None])

        # Print results table
        print(f"\n  {'Threshold':>10s}  {'Trades':>7s}  {'Mean P&L':>9s}  {'bps':>6s}  "
              f"{'WR%':>5s}  {'MaxDD':>7s}  {'Sharpe':>7s}  {'Filtered':>8s}", flush=True)
        print(f"  {'-'*10}  {'-'*7}  {'-'*9}  {'-'*6}  "
              f"{'-'*5}  {'-'*7}  {'-'*7}  {'-'*8}", flush=True)

        baseline_n = len(trades_by_thresh[None])
        results = {}

        for thresh in thresholds:
            trades = trades_by_thresh[thresh]
            m = compute_metrics(trades, f"vrr<{thresh}" if thresh else "BASELINE")
            results[thresh] = m

            label = "BASELINE" if thresh is None else f"vrr<{thresh}"
            n_t = m["n_trades"]
            if n_t > 0:
                filtered_pct = (1.0 - n_t / max(baseline_n, 1)) * 100.0 if thresh is not None else 0.0
                print(f"  {label:>10s}  {n_t:>7d}  {m['mean_pnl_pts']:>+9.2f}  "
                      f"{m['mean_pnl_bps']:>+6.2f}  {m['win_rate']:>5.1f}  "
                      f"{m['max_dd_pts']:>7.1f}  {m['sharpe_ann']:>+7.2f}  "
                      f"{filtered_pct:>7.1f}%", flush=True)
            else:
                print(f"  {label:>10s}  {n_t:>7d}  {'N/A':>9s}  {'N/A':>6s}  "
                      f"{'N/A':>5s}  {'N/A':>7s}  {'N/A':>7s}  {'100%':>8s}", flush=True)

        # Statistical tests (OOS only)
        if split_name == "OOS" and baseline_n >= 5:
            print(f"\n  Statistical Tests (one-tailed t-test: gated > ungated):", flush=True)
            baseline_pnl = np.array([t["pnl_net_pts"] for t in trades_by_thresh[None]])
            for thresh in thresholds:
                if thresh is None:
                    continue
                gated_trades = trades_by_thresh[thresh]
                if len(gated_trades) < 5:
                    print(f"    vrr<{thresh}: N too small ({len(gated_trades)})", flush=True)
                    continue
                gated_pnl = np.array([t["pnl_net_pts"] for t in gated_trades])
                from scipy.stats import ttest_ind
                # One-tailed: gated mean > ungated mean
                t_stat, p_two = ttest_ind(gated_pnl, baseline_pnl, equal_var=False)
                p_one = p_two / 2.0 if t_stat > 0 else 1.0 - p_two / 2.0
                mean_diff = float(np.mean(gated_pnl)) - float(np.mean(baseline_pnl))
                print(f"    vrr<{thresh}: diff={mean_diff:+.2f} pts, t={t_stat:+.3f}, "
                      f"p(one-tailed)={p_one:.4f}"
                      f"{'  *' if p_one < 0.10 else ''}", flush=True)

    print(f"\n{'='*80}", flush=True)
    print("BACKTEST COMPLETE", flush=True)
    print(f"{'='*80}", flush=True)


if __name__ == "__main__":
    main()
