"""P2-lite Walk-Forward Out-of-Sample Validation.

Runs 3-fold walk-forward validation of the OFI single-side quoting + inventory
decay strategy (Scenario B) on 12 days of TXFD6 L1 data.

Fold 1: Train Jan 27-30 (4d) -> Test Feb 3-4 (2d)
Fold 2: Train Jan 28-Feb 4 (5d) -> Test Feb 5-6 (2d)
Fold 3: Train Feb 3-6, Mar 19-20 (6d) -> Test Mar 23-24 (2d)

Key findings on data regimes:
- OFI EMA magnitudes vary ~20x across periods. Thresholds are z-scored
  per-day for regime-adaptive comparison.
- Spread regimes differ dramatically: Jan/Feb has median spread ~200 pts
  (min 22-37), March has median ~5 pts (min 1). The passive fill model
  (buy at best_bid, fill when ask<=bid) cannot fill in wide-spread regimes.
- Two fill modes are supported:
  (1) "passive" (original): quote at BBO, fill when opposite side crosses.
  (2) "mid_cross": quote at mid-price +/- 1pt, fill when mid crosses.
      This works across spread regimes but is more aggressive / less realistic.

Usage:
    uv run python research/tools/backtest_mm_p2_walkforward.py

Outputs: outputs/team_artifacts/alpha-research/stage4_mm_p2_walkforward.json
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger("backtest.mm_p2_walkforward")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "research" / "data" / "raw" / "txfd6"
_OUT_PATH = (
    _REPO_ROOT
    / "outputs"
    / "team_artifacts"
    / "alpha-research"
    / "stage4_mm_p2_walkforward.json"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICK_SIZE_POINTS: int = 1
POINT_VALUE_NTD: int = 10  # Mini-TAIEX: 1 point = 10 NTD
RT_COST_NTD: int = 5       # round-trip cost
LATENCY_TICKS: int = 1     # 1 tick latency
SAMPLE_INTERVAL: int = 1000
_EPS: float = 1e-12

# Fixed Scenario B params
MAX_HOLD_TICKS: int = 20
MAX_POS: int = 5
EMA_ALPHA: float = 0.1

# Entry threshold sweep (in z-score units of OFI std)
ENTRY_SWEEP: list[float] = [1.0, 1.5, 2.0, 2.5, 3.0]
EXIT_RATIO: float = 0.33  # exit z = entry_z * EXIT_RATIO

# Walk-forward fold definitions
FOLDS: list[dict[str, list[str]]] = [
    {
        "name": "Fold 1",
        "train": ["2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30"],
        "test": ["2026-02-03", "2026-02-04"],
    },
    {
        "name": "Fold 2",
        "train": [
            "2026-01-28", "2026-01-29", "2026-01-30",
            "2026-02-03", "2026-02-04",
        ],
        "test": ["2026-02-05", "2026-02-06"],
    },
    {
        "name": "Fold 3",
        "train": [
            "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06",
            "2026-03-19", "2026-03-20",
        ],
        "test": ["2026-03-23", "2026-03-24"],
    },
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_day(date_str: str) -> np.ndarray | None:
    """Load a single day's L1 data. Returns None if file missing."""
    path = _DATA_DIR / f"TXFD6_{date_str}_l1.npy"
    if not path.exists():
        logger.warning("data_file_missing", path=str(path))
        return None
    data = np.load(str(path))
    if len(data) < 100:
        logger.warning("data_file_too_small", path=str(path), rows=len(data))
        return None
    return data


def _load_days(dates: list[str]) -> np.ndarray | None:
    """Load and concatenate multiple days. Returns None if no data."""
    arrays: list[np.ndarray] = []
    for d in dates:
        arr = _load_day(d)
        if arr is not None:
            arrays.append(arr)
    if not arrays:
        return None
    return np.concatenate(arrays)


# ---------------------------------------------------------------------------
# OFI computation (vectorized, from P2-lite)
# ---------------------------------------------------------------------------

def _compute_ofi_vectorized(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
) -> np.ndarray:
    """Vectorized L1 OFI with Lee-Ready price-level adjustments."""
    n = len(bid_px)
    ofi = np.zeros(n, dtype=np.float64)

    bid_px_diff = np.diff(bid_px)
    ask_px_diff = np.diff(ask_px)
    bid_qty_diff = np.diff(bid_qty)
    ask_qty_diff = np.diff(ask_qty)

    delta_bid = np.where(
        bid_px_diff > 0, bid_qty[1:],
        np.where(bid_px_diff < 0, -bid_qty[:-1], bid_qty_diff),
    )

    delta_ask = np.where(
        ask_px_diff < 0, ask_qty[1:],
        np.where(ask_px_diff > 0, -ask_qty[:-1], ask_qty_diff),
    )

    ofi[1:] = delta_bid - delta_ask
    return ofi


def _ema_array(arr: np.ndarray, alpha: float) -> np.ndarray:
    """Compute EMA over array (sequential)."""
    n = len(arr)
    out = np.empty(n, dtype=np.float64)
    val = 0.0
    one_minus_a = 1.0 - alpha
    for i in range(n):
        val = alpha * arr[i] + one_minus_a * val
        out[i] = val
    return out


# ---------------------------------------------------------------------------
# Precompute with per-day z-scoring
# ---------------------------------------------------------------------------

def _precompute_days(dates: list[str]) -> dict[str, Any] | None:
    """Precompute arrays with per-day z-scored OFI.

    Each day's OFI EMA is normalized by its own standard deviation so that
    thresholds are comparable across regimes with different OFI magnitudes.
    """
    all_mid: list[np.ndarray] = []
    all_bb: list[np.ndarray] = []
    all_ba: list[np.ndarray] = []
    all_ofi_z: list[np.ndarray] = []
    day_stats: list[dict[str, Any]] = []

    for date_str in dates:
        day_data = _load_day(date_str)
        if day_data is None:
            continue

        bid_px = day_data["bid_px"].astype(np.float64)
        ask_px = day_data["ask_px"].astype(np.float64)
        bid_qty = day_data["bid_qty"].astype(np.float64)
        ask_qty = day_data["ask_qty"].astype(np.float64)

        mid = np.round((bid_px + ask_px) / 2.0).astype(np.int64)
        bb = np.round(bid_px).astype(np.int64)
        ba = np.round(ask_px).astype(np.int64)

        ofi_raw = _compute_ofi_vectorized(bid_px, ask_px, bid_qty, ask_qty)
        ofi_ema = _ema_array(ofi_raw, EMA_ALPHA)

        ofi_std = float(np.std(ofi_ema))
        if ofi_std < _EPS:
            ofi_std = 1.0
        ofi_z = ofi_ema / ofi_std

        spread = ba - bb
        median_spread = float(np.median(spread))

        all_mid.append(mid)
        all_bb.append(bb)
        all_ba.append(ba)
        all_ofi_z.append(ofi_z)
        day_stats.append({
            "date": date_str,
            "rows": len(day_data),
            "ofi_std": round(ofi_std, 4),
            "median_spread": round(median_spread, 1),
        })

    if not all_mid:
        return None

    total = sum(len(a) for a in all_mid)
    return {
        "n": total,
        "mid": np.concatenate(all_mid),
        "best_bid": np.concatenate(all_bb),
        "best_ask": np.concatenate(all_ba),
        "ofi_z": np.concatenate(all_ofi_z),
        "day_stats": day_stats,
    }


# ---------------------------------------------------------------------------
# Simulation core (Scenario B: OFI single-side + inventory decay)
# ---------------------------------------------------------------------------

def _sim_core(
    ofi: np.ndarray,
    mid: np.ndarray,
    best_bid: np.ndarray,
    best_ask: np.ndarray,
    entry_thr: float,
    exit_thr: float,
    max_hold: int,
    max_pos: int,
    *,
    fill_mode: str = "passive",
) -> dict[str, Any]:
    """Core simulation loop with inventory decay (Scenario B).

    ofi is z-scored, so entry_thr/exit_thr are in z-score units.

    fill_mode:
      "passive": quote at BBO, fill when opposite side crosses (original).
      "mid_cross": quote at mid +/- 1pt, fill when mid crosses level.
                   Works in wide-spread regimes but more aggressive.
    """
    n = len(ofi)
    warmup = max(50, LATENCY_TICKS + 2)

    pos = 0
    rpnl = 0
    peak_eq = 0
    max_dd = 0
    n_entries = 0
    n_exits = 0
    n_time_exits = 0
    entry_tick = 0
    entry_px = 0

    p_side = 0
    p_price = 0
    p_tick = 0
    p_exit = False
    p_exit_tick = 0

    rt_pnls: list[int] = []
    rt_holds: list[int] = []
    eq_samples: list[int] = []

    pv = POINT_VALUE_NTD
    rc = RT_COST_NTD
    lat = LATENCY_TICKS
    si = SAMPLE_INTERVAL
    use_mid = fill_mode == "mid_cross"

    for i in range(warmup, n):
        bb = int(best_bid[i])
        ba = int(best_ask[i])
        mi = int(mid[i])
        ov = float(ofi[i])

        # --- Fill pending exit ---
        if p_exit:
            if i >= p_exit_tick + lat:
                pnl_pts = (mi - entry_px) * pos
                pnl_ntd = pnl_pts * pv - rc * abs(pos)
                rpnl += pnl_ntd
                rt_pnls.append(pnl_ntd)
                rt_holds.append(i - entry_tick)
                n_exits += abs(pos)
                pos = 0
                p_exit = False
                p_side = 0
            continue

        # --- Fill pending entry ---
        if p_side != 0 and i >= p_tick + lat:
            filled = False
            if use_mid:
                # Mid-cross: fill when mid reaches/crosses quote level
                if p_side == 1 and mi <= p_price:
                    filled = True
                elif p_side == -1 and mi >= p_price:
                    filled = True
            else:
                # Passive: fill when opposite side crosses quote level
                if p_side == 1 and ba <= p_price:
                    filled = True
                elif p_side == -1 and bb >= p_price:
                    filled = True

            if filled:
                if p_side == 1 and pos < max_pos:
                    pos += 1
                    entry_tick = i
                    entry_px = p_price
                    n_entries += 1
                elif p_side == -1 and pos > -max_pos:
                    pos -= 1
                    entry_tick = i
                    entry_px = p_price
                    n_entries += 1
            p_side = 0

        # --- Exit logic ---
        if pos != 0:
            held = i - entry_tick

            # Time exit
            if held > max_hold:
                p_exit = True
                p_exit_tick = i
                n_time_exits += 1
                continue

            # Inventory decay (quadratic)
            df = 1.0 - (held / max_hold) ** 2
            eff_exit = exit_thr * max(df, 0.05)

            # Signal reversal
            if pos > 0 and ov < -eff_exit:
                p_exit = True
                p_exit_tick = i
                continue
            if pos < 0 and ov > eff_exit:
                p_exit = True
                p_exit_tick = i
                continue

        # --- Entry logic (only when flat) ---
        if pos == 0 and p_side == 0:
            if ov > entry_thr:
                p_side = 1
                if use_mid:
                    p_price = mi - 1  # buy below mid
                else:
                    p_price = bb      # buy at best bid
                p_tick = i
            elif ov < -entry_thr:
                p_side = -1
                if use_mid:
                    p_price = mi + 1  # sell above mid
                else:
                    p_price = ba      # sell at best ask
                p_tick = i

        # --- Equity sample ---
        if i % si == 0:
            unreal = pos * (mi - entry_px) * pv if pos != 0 else 0
            eq = rpnl + unreal
            eq_samples.append(eq)
            if eq > peak_eq:
                peak_eq = eq
            dd = peak_eq - eq
            if dd > max_dd:
                max_dd = dd

    # Final flatten
    if pos != 0:
        mi = int(mid[n - 1])
        pnl_pts = (mi - entry_px) * pos
        pnl_ntd = pnl_pts * pv - rc * abs(pos)
        rpnl += pnl_ntd
        rt_pnls.append(pnl_ntd)
        rt_holds.append(n - 1 - entry_tick)

    return _build_metrics(
        rpnl, rt_pnls, rt_holds, eq_samples,
        max_dd, n_entries, n_exits, n_time_exits, n,
    )


def _build_metrics(
    realized_pnl_ntd: int,
    rt_pnls: list[int],
    rt_holds: list[int],
    equity_samples: list[int],
    max_drawdown_ntd: int,
    n_entries: int,
    n_exits: int,
    n_time_exits: int,
    n_ticks: int,
) -> dict[str, Any]:
    """Build standardized metrics dict."""
    n_rt = len(rt_pnls)
    if n_rt > 0:
        wins = sum(1 for p in rt_pnls if p > 0)
        win_rate = wins / n_rt
        mean_pnl = sum(rt_pnls) / n_rt
        mean_hold = sum(rt_holds) / n_rt
    else:
        win_rate = 0.0
        mean_pnl = 0.0
        mean_hold = 0.0

    # Sharpe from equity samples
    sharpe = 0.0
    if len(equity_samples) > 10:
        eq = np.array(equity_samples, dtype=np.float64)
        rets = np.diff(eq)
        if len(rets) > 1:
            mu = float(np.mean(rets))
            std = float(np.std(rets))
            if std > _EPS:
                samples_per_day = (6.5 * 3600) / (SAMPLE_INTERVAL / 3.7)
                sharpe = (mu / std) * math.sqrt(samples_per_day * 252)

    return {
        "total_pnl_ntd": int(realized_pnl_ntd),
        "sharpe": round(sharpe, 4),
        "max_drawdown_ntd": int(max_drawdown_ntd),
        "n_round_trips": n_rt,
        "n_entries": n_entries,
        "n_exits": n_exits,
        "n_time_exits": n_time_exits,
        "win_rate": round(win_rate, 4),
        "mean_pnl_per_rt_ntd": round(mean_pnl, 2),
        "mean_hold_ticks": round(mean_hold, 2),
        "n_ticks": n_ticks,
    }


# ---------------------------------------------------------------------------
# Walk-forward engine
# ---------------------------------------------------------------------------

def _run_sim_on_dates(
    dates: list[str], entry_z: float, *, fill_mode: str = "passive",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Precompute per-day z-scored OFI + simulate.

    Returns (metrics_or_None, day_stats).
    """
    pc = _precompute_days(dates)
    if pc is None:
        return None, []
    exit_z = entry_z * EXIT_RATIO
    metrics = _sim_core(
        pc["ofi_z"], pc["mid"], pc["best_bid"], pc["best_ask"],
        entry_z, exit_z, MAX_HOLD_TICKS, MAX_POS,
        fill_mode=fill_mode,
    )
    return metrics, pc["day_stats"]


def _optimize_entry_threshold(
    train_dates: list[str], *, fill_mode: str = "passive",
) -> tuple[float, dict[str, Any]]:
    """Sweep entry z-score on training data. Return best threshold + metrics."""
    best_thr = ENTRY_SWEEP[0]
    best_sharpe = -999.0
    best_metrics: dict[str, Any] = {}

    for thr in ENTRY_SWEEP:
        m, _ = _run_sim_on_dates(train_dates, thr, fill_mode=fill_mode)
        if m is None:
            continue
        logger.debug(
            "train_sweep",
            entry_z=thr,
            sharpe=m["sharpe"],
            pnl=m["total_pnl_ntd"],
            rts=m["n_round_trips"],
        )
        if m["sharpe"] > best_sharpe:
            best_sharpe = m["sharpe"]
            best_thr = thr
            best_metrics = m

    return best_thr, best_metrics


def _run_folds(
    fill_mode: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Run all folds with a given fill mode. Returns (fold_results, summary, is_vs_oos)."""
    fold_results: list[dict[str, Any]] = []

    for fold_def in FOLDS:
        fold_name = fold_def["name"]
        train_dates = fold_def["train"]
        test_dates = fold_def["test"]

        logger.info(
            "fold_start",
            fold=fold_name,
            fill_mode=fill_mode,
            train_dates=train_dates,
            test_dates=test_dates,
        )

        # Quick data check
        train_data = _load_days(train_dates)
        test_data = _load_days(test_dates)

        if train_data is None or test_data is None:
            logger.error("fold_data_missing", fold=fold_name)
            fold_results.append({
                "fold": fold_name,
                "status": "SKIP_NO_DATA",
                "train_dates": train_dates,
                "test_dates": test_dates,
            })
            continue

        train_rows = len(train_data)
        test_rows = len(test_data)
        del train_data, test_data

        logger.info(
            "fold_data_loaded", fold=fold_name,
            train_rows=train_rows, test_rows=test_rows,
        )

        # Optimize on training data
        best_thr, train_metrics = _optimize_entry_threshold(
            train_dates, fill_mode=fill_mode,
        )
        logger.info(
            "fold_train_optimal",
            fold=fold_name,
            best_entry_z=best_thr,
            train_sharpe=train_metrics["sharpe"],
            train_pnl=train_metrics["total_pnl_ntd"],
            train_rts=train_metrics["n_round_trips"],
        )

        # Test with optimized threshold
        test_metrics, test_day_stats = _run_sim_on_dates(
            test_dates, best_thr, fill_mode=fill_mode,
        )
        if test_metrics is None:
            logger.error("fold_test_failed", fold=fold_name)
            continue

        logger.info(
            "fold_test_result",
            fold=fold_name,
            oos_sharpe=test_metrics["sharpe"],
            oos_pnl=test_metrics["total_pnl_ntd"],
            oos_win_rate=test_metrics["win_rate"],
            oos_rts=test_metrics["n_round_trips"],
        )

        fold_results.append({
            "fold": fold_name,
            "status": "OK",
            "train_dates": train_dates,
            "test_dates": test_dates,
            "optimal_entry_z": best_thr,
            "optimal_exit_z": round(best_thr * EXIT_RATIO, 4),
            "train_metrics": train_metrics,
            "test_metrics": test_metrics,
            "test_day_stats": test_day_stats,
        })

    # Summary
    ok_folds = [f for f in fold_results if f["status"] == "OK"]
    oos_sharpes = [f["test_metrics"]["sharpe"] for f in ok_folds]
    oos_pnls = [f["test_metrics"]["total_pnl_ntd"] for f in ok_folds]
    oos_win_rates = [f["test_metrics"]["win_rate"] for f in ok_folds]
    oos_rts = [f["test_metrics"]["n_round_trips"] for f in ok_folds]

    n_ok = len(oos_sharpes)
    summary: dict[str, Any] = {
        "n_folds_ok": n_ok,
        "n_folds_total": len(FOLDS),
    }
    if n_ok > 0:
        summary["mean_oos_sharpe"] = round(float(np.mean(oos_sharpes)), 4)
        summary["std_oos_sharpe"] = round(float(np.std(oos_sharpes)), 4)
        summary["mean_oos_win_rate"] = round(float(np.mean(oos_win_rates)), 4)
        summary["total_oos_pnl_ntd"] = int(sum(oos_pnls))
        summary["total_oos_round_trips"] = int(sum(oos_rts))
        summary["all_folds_profitable"] = all(p > 0 for p in oos_pnls)
        summary["all_folds_positive_sharpe"] = all(s > 0 for s in oos_sharpes)

    # IS vs OOS
    all_train_dates = sorted(set(
        d for fold_def in FOLDS for d in fold_def["train"]
    ))
    all_test_dates = sorted(set(
        d for fold_def in FOLDS for d in fold_def["test"]
    ))

    is_vs_oos: dict[str, Any] = {}
    best_global_thr, is_metrics = _optimize_entry_threshold(
        all_train_dates, fill_mode=fill_mode,
    )
    oos_global_metrics, _ = _run_sim_on_dates(
        all_test_dates, best_global_thr, fill_mode=fill_mode,
    )

    if oos_global_metrics is not None:
        is_sharpe = is_metrics["sharpe"]
        oos_sharpe = oos_global_metrics["sharpe"]
        degradation = oos_sharpe / is_sharpe if abs(is_sharpe) > _EPS else 0.0

        is_vs_oos = {
            "best_global_entry_z": best_global_thr,
            "best_global_exit_z": round(best_global_thr * EXIT_RATIO, 4),
            "is_train_dates": all_train_dates,
            "oos_test_dates": all_test_dates,
            "is_sharpe": is_metrics["sharpe"],
            "is_pnl_ntd": is_metrics["total_pnl_ntd"],
            "is_round_trips": is_metrics["n_round_trips"],
            "is_win_rate": is_metrics["win_rate"],
            "oos_sharpe": oos_global_metrics["sharpe"],
            "oos_pnl_ntd": oos_global_metrics["total_pnl_ntd"],
            "oos_round_trips": oos_global_metrics["n_round_trips"],
            "oos_win_rate": oos_global_metrics["win_rate"],
            "oos_max_dd_ntd": oos_global_metrics["max_drawdown_ntd"],
            "degradation_ratio": round(degradation, 4),
        }

    return fold_results, summary, is_vs_oos


def run_walkforward() -> dict[str, Any]:
    """Execute walk-forward validation with both fill modes."""
    logger.info("walk_forward_start", n_folds=len(FOLDS))
    t_start = time.monotonic()

    # --- Run with both fill modes ---
    results_by_mode: dict[str, Any] = {}
    for mode in ["passive", "mid_cross"]:
        logger.info("fill_mode_start", fill_mode=mode)
        folds, summary, is_vs_oos = _run_folds(mode)
        results_by_mode[mode] = {
            "folds": folds,
            "summary": summary,
            "is_vs_oos": is_vs_oos,
        }

    elapsed = time.monotonic() - t_start
    logger.info("walk_forward_done", elapsed_s=round(elapsed, 2))

    return {
        "strategy": "P2-lite Scenario B (OFI single-side + inventory decay)",
        "threshold_mode": "z-scored (per-day OFI std normalization)",
        "fixed_params": {
            "exit_ratio": EXIT_RATIO,
            "max_hold_ticks": MAX_HOLD_TICKS,
            "max_pos": MAX_POS,
            "ema_alpha": EMA_ALPHA,
            "inventory_decay": True,
            "latency_ticks": LATENCY_TICKS,
            "rt_cost_ntd": RT_COST_NTD,
            "point_value_ntd": POINT_VALUE_NTD,
        },
        "entry_z_sweep": ENTRY_SWEEP,
        "fill_modes": results_by_mode,
        "elapsed_s": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------

def _print_mode_results(
    mode_name: str,
    mode_data: dict[str, Any],
) -> None:
    """Print fold-by-fold table for one fill mode."""
    print(f"\n{'=' * 90}")
    print(f"Fill Mode: {mode_name.upper()}")
    print("=" * 90)

    header = (
        f"{'Fold':<8} {'Train Dates':<40} {'Test Dates':<20} "
        f"{'EntZ':>5} {'ExZ':>5} {'TrnSR':>7} {'OosSR':>7} "
        f"{'OosPnL':>7} {'OosDD':>6} {'RTs':>5} "
        f"{'Win%':>6} {'PnL/RT':>7}"
    )
    print(header)
    print("-" * len(header))

    for fold in mode_data["folds"]:
        if fold["status"] != "OK":
            print(f"{fold['fold']:<8} SKIPPED (no data)")
            continue

        train_str = ", ".join(d[5:] for d in fold["train_dates"])
        test_str = ", ".join(d[5:] for d in fold["test_dates"])
        tm = fold["train_metrics"]
        om = fold["test_metrics"]

        # Flag zero-trade folds
        rt_flag = " *" if om["n_round_trips"] == 0 else ""

        print(
            f"{fold['fold']:<8} {train_str:<40} {test_str:<20} "
            f"{fold['optimal_entry_z']:>5.1f} "
            f"{fold['optimal_exit_z']:>5.2f} "
            f"{tm['sharpe']:>+7.2f} "
            f"{om['sharpe']:>+7.2f} "
            f"{om['total_pnl_ntd']:>7d} "
            f"{om['max_drawdown_ntd']:>6d} "
            f"{om['n_round_trips']:>5d}{rt_flag} "
            f"{om['win_rate']:>5.1%} "
            f"{om['mean_pnl_per_rt_ntd']:>7.1f}"
        )

        # Print spread info if zero trades
        if om["n_round_trips"] == 0 and "test_day_stats" in fold:
            spreads = [ds["median_spread"] for ds in fold["test_day_stats"]]
            print(f"         (zero fills: test median spreads = "
                  f"{[f'{s:.0f}' for s in spreads]})")

    # Summary
    s = mode_data["summary"]
    print()
    print(f"--- Summary ({mode_name}) ---")
    if s["n_folds_ok"] > 0:
        print(f"  Folds OK:                {s['n_folds_ok']}/{s['n_folds_total']}")
        print(f"  Mean OOS Sharpe:         {s['mean_oos_sharpe']:+.4f}")
        print(f"  Std OOS Sharpe:          {s['std_oos_sharpe']:.4f}")
        print(f"  Mean OOS Win Rate:       {s['mean_oos_win_rate']:.1%}")
        print(f"  Total OOS PnL (NTD):     {s['total_oos_pnl_ntd']:,d}")
        print(f"  Total OOS Round Trips:   {s['total_oos_round_trips']:,d}")
        print(f"  All folds profitable:    {'YES' if s['all_folds_profitable'] else 'NO'}")
        print(f"  All folds Sharpe > 0:    {'YES' if s['all_folds_positive_sharpe'] else 'NO'}")

    # IS vs OOS
    iv = mode_data.get("is_vs_oos", {})
    if iv:
        print()
        print(f"--- IS vs OOS ({mode_name}) ---")
        print(f"  Best global entry_z: {iv['best_global_entry_z']} "
              f"(exit_z: {iv['best_global_exit_z']})")
        print(f"  IS Sharpe:           {iv['is_sharpe']:+.4f}  "
              f"(on {len(iv['is_train_dates'])} days)")
        print(f"  OOS Sharpe:          {iv['oos_sharpe']:+.4f}  "
              f"(on {len(iv['oos_test_dates'])} days)")
        print(f"  Degradation (OOS/IS): {iv['degradation_ratio']:.4f}")
        print(f"  IS PnL:  {iv['is_pnl_ntd']:>8,d} NTD  "
              f"({iv['is_round_trips']} RTs, {iv['is_win_rate']:.1%} win)")
        print(f"  OOS PnL: {iv['oos_pnl_ntd']:>8,d} NTD  "
              f"({iv['oos_round_trips']} RTs, {iv['oos_win_rate']:.1%} win)")
        print(f"  OOS Max DD: {iv['oos_max_dd_ntd']:,d} NTD")


def _print_results(results: dict[str, Any]) -> None:
    """Print full results."""
    print("\n" + "=" * 90)
    print("P2-lite Walk-Forward OOS Validation")
    print("=" * 90)
    print(f"Strategy: {results['strategy']}")
    print(f"Threshold mode: {results['threshold_mode']}")
    print(f"Entry z-score sweep: {results['entry_z_sweep']}")
    fp = results["fixed_params"]
    print(f"Fixed params: exit_ratio={fp['exit_ratio']}, hold={fp['max_hold_ticks']}, "
          f"max_pos={fp['max_pos']}, ema={fp['ema_alpha']}, decay=True")

    for mode_name, mode_data in results["fill_modes"].items():
        _print_mode_results(mode_name, mode_data)

    # Overall verdict
    print()
    print("=" * 90)
    print("OVERALL VERDICT")
    print("=" * 90)

    passive_s = results["fill_modes"]["passive"]["summary"]
    midcross_s = results["fill_modes"]["mid_cross"]["summary"]

    # Passive mode assessment
    passive_rts = passive_s.get("total_oos_round_trips", 0)
    midcross_rts = midcross_s.get("total_oos_round_trips", 0)

    print(f"\n  Passive fill mode:  {passive_rts} OOS round trips, "
          f"mean Sharpe {passive_s.get('mean_oos_sharpe', 0):+.2f}")
    print(f"  Mid-cross fill mode: {midcross_rts} OOS round trips, "
          f"mean Sharpe {midcross_s.get('mean_oos_sharpe', 0):+.2f}")

    # Check regime dependency
    passive_ok = [f for f in results["fill_modes"]["passive"]["folds"]
                  if f["status"] == "OK" and f["test_metrics"]["n_round_trips"] > 0]
    midcross_ok = [f for f in results["fill_modes"]["mid_cross"]["folds"]
                   if f["status"] == "OK" and f["test_metrics"]["n_round_trips"] > 0]

    print(f"\n  Passive: {len(passive_ok)}/3 folds with trades")
    print(f"  Mid-cross: {len(midcross_ok)}/3 folds with trades")

    if len(midcross_ok) >= 2:
        all_pos = all(
            f["test_metrics"]["sharpe"] > 0 for f in midcross_ok
        )
        mean_sr = np.mean([f["test_metrics"]["sharpe"] for f in midcross_ok])
        if all_pos and mean_sr > 1.0:
            print("\n  VERDICT: Alpha shows PROMISING OOS signal across regimes")
            print("           (via mid-cross fill model)")
        elif mean_sr > 0:
            print("\n  VERDICT: Alpha shows WEAK OOS persistence")
        else:
            print("\n  VERDICT: Alpha FAILS OOS")
    else:
        print("\n  VERDICT: Alpha is REGIME-DEPENDENT")
        print("           Only works in tight-spread regimes (March 2026)")
        print("           Jan/Feb data has spreads 22-200+ pts -- no passive fills possible")
        if len(passive_ok) >= 1 and passive_ok[0]["test_metrics"]["sharpe"] > 0:
            print(f"           Fold 3 (tight spreads) OOS Sharpe: "
                  f"{passive_ok[0]['test_metrics']['sharpe']:+.2f}")
    print("=" * 90)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Run walk-forward validation and save results."""
    results = run_walkforward()

    _print_results(results)

    # Save JSON
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_OUT_PATH, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info("results_saved", path=str(_OUT_PATH))
    print(f"Results saved to: {_OUT_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
