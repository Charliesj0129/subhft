"""Gate C: PnL backtest for mlofi_gradient (gradient-only, L32_O2).

Simulates a simple mean-reversion strategy driven by the mlofi_gradient signal:
- Signal < -threshold: BUY (price expected to rise)
- Signal > +threshold: SELL (price expected to fall)
- Hold period: fixed (1s or 5s)
- Position sizing: 1 unit per signal
- Fees: TWSE 0.1425% commission (buy+sell) + 0.3% tax on sells
- Latency: 36ms P95 applied to entry (fill at next tick after 36ms)

Walk-forward: 3 folds of ~6/6/5 days.

Gate C pass criteria: Sharpe > 1.0, max_dd < 10%.
"""

from __future__ import annotations

import sys, json, math, time
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats as scipy_stats

_HERE = Path(__file__).resolve()
_RESEARCH_ROOT = _HERE.parent.parent
_PROJECT_ROOT = _RESEARCH_ROOT.parent
for _p in (_PROJECT_ROOT, _RESEARCH_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from research.alphas.mlofi_gradient.impl import MlofiGradientAlpha, _WARMUP_TICKS

# ---- Constants ----
LATENCY_NS = 36_000_000  # 36ms Shioaji P95
DAY_GAP_NS = 4 * 3_600_000_000_000

# TWSE fees
COMMISSION_RATE = 0.001425  # 0.1425% each way
TAX_RATE = 0.003           # 0.3% on sells only (stocks)
# Total round-trip cost = 2 * commission + tax = 0.585%
ROUND_TRIP_COST = 2 * COMMISSION_RATE + TAX_RATE

# Strategy params
SIGNAL_THRESHOLD = 0.05    # enter when |signal| > threshold
HOLD_NS = 1_000_000_000   # 1s hold (matches best IC horizon)
MAX_POSITION = 1           # 1 unit max

def split_days(ts):
    gaps = np.diff(ts)
    bounds = np.where(gaps > DAY_GAP_NS)[0] + 1
    starts = np.concatenate([[0], bounds])
    ends = np.concatenate([bounds, [len(ts)]])
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def replay_signals(data):
    """Replay mlofi_gradient and return signals array."""
    n = len(data)
    signals = np.empty(n, dtype=np.float64)
    alpha = MlofiGradientAlpha()
    for i in range(n):
        b = np.column_stack([data[i]["bids_price"], data[i]["bids_vol"]]).astype(np.float64)
        a = np.column_stack([data[i]["asks_price"], data[i]["asks_vol"]]).astype(np.float64)
        signals[i] = alpha.update(bids=b, asks=a)
    return signals


def simulate_pnl(
    timestamps: np.ndarray,
    mid_prices: np.ndarray,
    signals: np.ndarray,
    day_segments: list[tuple[int, int]],
    threshold: float = SIGNAL_THRESHOLD,
    hold_ns: int = HOLD_NS,
) -> dict[str, Any]:
    """Simulate PnL with fixed holding period and TWSE fees.

    Strategy:
    - When signal < -threshold at tick i, BUY at mid[j] where j is first tick
      after ts[i] + LATENCY_NS (simulating execution delay)
    - Hold for hold_ns, then EXIT at mid[k] where k is first tick after entry + hold_ns
    - When signal > +threshold, SELL (short) similarly
    - No overlapping positions (flat between trades)
    """
    n = len(timestamps)
    trades: list[dict[str, Any]] = []

    for ds, de in day_segments:
        position = 0  # 0=flat, +1=long, -1=short
        entry_price = 0.0
        exit_target_ns = 0

        for i in range(ds + _WARMUP_TICKS, de):
            # Check if we need to exit
            if position != 0 and timestamps[i] >= exit_target_ns:
                exit_price = mid_prices[i]
                if exit_price > 0 and entry_price > 0:
                    gross_ret = position * (exit_price - entry_price) / entry_price
                    net_ret = gross_ret - ROUND_TRIP_COST
                    trades.append({
                        "entry_ts": entry_ts,
                        "exit_ts": int(timestamps[i]),
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "side": position,
                        "gross_ret": gross_ret,
                        "net_ret": net_ret,
                    })
                position = 0
                entry_price = 0.0

            # Check for entry signal (only when flat)
            if position == 0 and signals[i] != 0.0:
                # Find execution tick (first tick after latency)
                exec_ts = timestamps[i] + LATENCY_NS
                j = i
                while j < de and timestamps[j] < exec_ts:
                    j += 1
                if j >= de:
                    continue

                if signals[i] < -threshold:
                    # BUY signal (contrarian: negative gradient -> price will rise)
                    position = 1
                    entry_price = mid_prices[j]
                    entry_ts = int(timestamps[j])
                    exit_target_ns = timestamps[j] + hold_ns
                elif signals[i] > threshold:
                    # SELL signal (contrarian: positive gradient -> price will fall)
                    position = -1
                    entry_price = mid_prices[j]
                    entry_ts = int(timestamps[j])
                    exit_target_ns = timestamps[j] + hold_ns

    if not trades:
        return {"n_trades": 0, "sharpe": 0, "sortino": 0, "max_dd": 0, "pnl": 0, "win_rate": 0}

    # Compute metrics
    net_rets = np.array([t["net_ret"] for t in trades])
    gross_rets = np.array([t["gross_ret"] for t in trades])

    n_trades = len(trades)
    total_pnl_bps = float(np.sum(net_rets)) * 10000
    mean_ret = float(np.mean(net_rets))
    std_ret = float(np.std(net_rets)) if len(net_rets) > 1 else 1e-10

    # Sharpe (annualized assuming ~4.5 hours trading, ~270 days/year)
    # Trades per day estimate
    trades_per_day = n_trades / max(1, len(day_segments))
    daily_ret = mean_ret * trades_per_day
    daily_std = std_ret * np.sqrt(trades_per_day)
    sharpe = (daily_ret / daily_std * np.sqrt(252)) if daily_std > 0 else 0.0

    # Sortino
    downside = net_rets[net_rets < 0]
    downside_std = float(np.std(downside)) if len(downside) > 1 else 1e-10
    daily_downside = downside_std * np.sqrt(trades_per_day)
    sortino = (daily_ret / daily_downside * np.sqrt(252)) if daily_downside > 0 else 0.0

    # Max drawdown (cumulative PnL curve)
    cum_pnl = np.cumsum(net_rets)
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = running_max - cum_pnl
    max_dd = float(np.max(drawdowns)) * 100  # as percentage

    # Win rate and profit factor
    wins = net_rets[net_rets > 0]
    losses = net_rets[net_rets < 0]
    win_rate = len(wins) / n_trades * 100
    profit_factor = float(np.sum(wins) / abs(np.sum(losses))) if len(losses) > 0 and np.sum(losses) != 0 else float("inf")

    # Long/short breakdown
    long_trades = [t for t in trades if t["side"] == 1]
    short_trades = [t for t in trades if t["side"] == -1]

    return {
        "n_trades": n_trades,
        "trades_per_day": round(trades_per_day, 1),
        "total_pnl_bps": round(total_pnl_bps, 2),
        "mean_ret_bps": round(mean_ret * 10000, 4),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_dd_pct": round(max_dd, 4),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "n_long": len(long_trades),
        "n_short": len(short_trades),
        "avg_gross_ret_bps": round(float(np.mean(gross_rets)) * 10000, 4),
        "round_trip_cost_bps": round(ROUND_TRIP_COST * 10000, 2),
    }


def run_walk_forward(
    data: np.ndarray,
    timestamps: np.ndarray,
    mid_prices: np.ndarray,
    signals: np.ndarray,
    day_segments: list[tuple[int, int]],
    n_folds: int = 3,
) -> list[dict[str, Any]]:
    """Walk-forward: split days into folds and run PnL on each."""
    n_days = len(day_segments)
    fold_size = n_days // n_folds
    fold_results = []

    for fold in range(n_folds):
        start_day = fold * fold_size
        end_day = start_day + fold_size if fold < n_folds - 1 else n_days
        fold_segs = day_segments[start_day:end_day]

        if not fold_segs:
            continue

        result = simulate_pnl(timestamps, mid_prices, signals, fold_segs)
        result["fold"] = fold
        result["days"] = f"{start_day}-{end_day}"
        result["n_days"] = end_day - start_day
        fold_results.append(result)

    return fold_results


def run_gate_c(symbols: list[str], data_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Run Gate C PnL backtest for all symbols."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "gate": "C",
        "alpha": "mlofi_gradient_grad_only_L32_O2",
        "strategy": "mean_reversion_fixed_hold",
        "params": {
            "signal_threshold": SIGNAL_THRESHOLD,
            "hold_ns": HOLD_NS,
            "hold_label": "1s",
            "latency_ns": LATENCY_NS,
            "commission_rate": COMMISSION_RATE,
            "tax_rate": TAX_RATE,
            "round_trip_cost_bps": round(ROUND_TRIP_COST * 10000, 2),
        },
        "pass_criteria": {"sharpe_min": 1.0, "max_dd_max_pct": 10.0},
        "results": {},
    }

    for sym in symbols:
        npy_path = data_dir / f"{sym}_l5.npy"
        if not npy_path.exists():
            print(f"  {sym}: data not found", flush=True)
            continue

        data = np.load(str(npy_path))
        ts = data["timestamp_ns"]
        mid = (data["bids_price"][:, 0].astype(np.float64) + data["asks_price"][:, 0].astype(np.float64)) / 2
        segs = split_days(ts)

        print(f"\n=== {sym} ({len(data):,} rows, {len(segs)} days) ===", flush=True)

        # Replay signals
        t0 = time.perf_counter()
        signals = replay_signals(data)
        print(f"  Signal replay: {time.perf_counter()-t0:.1f}s", flush=True)

        # Full period PnL
        full_result = simulate_pnl(ts, mid, signals, segs)
        print(f"  Full: {full_result['n_trades']} trades, Sharpe={full_result['sharpe']}, "
              f"PnL={full_result['total_pnl_bps']:.0f}bps, MaxDD={full_result['max_dd_pct']:.2f}%, "
              f"WinRate={full_result['win_rate_pct']:.1f}%", flush=True)

        # Walk-forward folds
        wf_results = run_walk_forward(data, ts, mid, signals, segs, n_folds=3)
        for wf in wf_results:
            print(f"  Fold {wf['fold']} (days {wf['days']}): {wf['n_trades']} trades, "
                  f"Sharpe={wf['sharpe']}, PnL={wf['total_pnl_bps']:.0f}bps", flush=True)

        # Gate C pass/fail
        passes = (full_result["sharpe"] > 1.0 and full_result["max_dd_pct"] < 10.0)

        # Also try different thresholds
        threshold_sweep = {}
        for thr in [0.01, 0.02, 0.05, 0.1, 0.2, 0.5]:
            thr_result = simulate_pnl(ts, mid, signals, segs, threshold=thr)
            threshold_sweep[str(thr)] = thr_result
            if thr_result["n_trades"] > 0:
                print(f"  Threshold={thr}: {thr_result['n_trades']} trades, "
                      f"Sharpe={thr_result['sharpe']}, PnL={thr_result['total_pnl_bps']:.0f}bps", flush=True)

        # Also try 5s hold
        result_5s = simulate_pnl(ts, mid, signals, segs, hold_ns=5_000_000_000)
        print(f"  Hold=5s: {result_5s['n_trades']} trades, Sharpe={result_5s['sharpe']}, "
              f"PnL={result_5s['total_pnl_bps']:.0f}bps", flush=True)

        report["results"][sym] = {
            "n_rows": len(data),
            "n_days": len(segs),
            "full": full_result,
            "walk_forward": wf_results,
            "threshold_sweep": threshold_sweep,
            "hold_5s": result_5s,
            "gate_c_pass": passes,
        }

    # Save
    json_path = output_dir / "gate_c_pnl_data.json"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved: {json_path}", flush=True)

    return report


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="2330,2317")
    parser.add_argument("--data-dir", default="research/data/l5_v2/")
    parser.add_argument("--out", default="outputs/team_artifacts/alpha-research/")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    run_gate_c(symbols, Path(args.data_dir), Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
