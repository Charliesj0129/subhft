"""VPIN Regime Switch Alpha — Real TXFD6 data backtest.

Loads real L1 BidAsk data exported from ClickHouse, converts to scaled int,
runs VpinRegimeSwitchAlpha in both tick-volume and depth-proxy modes, performs
auto-calibration via RegimeDetector.calibrate(), and evaluates signal quality.

Usage:
    uv run python research/tools/backtest_vpin_real.py \
        --data research/data/raw/txfd6/TXFD6_all_l1.npy

Outputs: outputs/team_artifacts/alpha-research/stage4_vpin_real_data.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import structlog

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from research.alphas.vpin_regime_switch.impl import (  # noqa: E402
    Regime,
    RegimeDetector,
    VpinRegimeSwitchAlpha,
)

logger = structlog.get_logger("backtest.vpin.real")

_DEFAULT_DATA = _REPO_ROOT / "research" / "data" / "raw" / "txfd6" / "TXFD6_all_l1.npy"
_OUT_PATH = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "stage4_vpin_real_data.json"

LATENCY_PROFILE = {
    "name": "shioaji_sim_p95_v2026-03-04",
    "submit_ms": 36.0,
    "modify_ms": 43.0,
    "cancel_ms": 47.0,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _correlation(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3 or len(y) < 3:
        return 0.0
    sx, sy = x.std(), y.std()
    if sx < 1e-15 or sy < 1e-15:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _rolling_sharpe(returns: np.ndarray, window: int = 200) -> float:
    if len(returns) < window:
        if returns.std() < 1e-15:
            return 0.0
        return float(returns.mean() / returns.std()) * math.sqrt(252 * 100)
    sharpes: list[float] = []
    for i in range(0, len(returns) - window + 1, window // 2):
        chunk = returns[i : i + window]
        std = chunk.std()
        if std > 1e-15:
            sharpes.append(float(chunk.mean() / std))
    if not sharpes:
        return 0.0
    avg = sum(sharpes) / len(sharpes)
    return avg * math.sqrt(252 * 100)


def _percentiles(arr: np.ndarray) -> dict[str, float]:
    """Compute summary percentiles for a 1-D array."""
    valid = arr[np.isfinite(arr)]
    if len(valid) == 0:
        return {}
    return {
        "mean": float(np.mean(valid)),
        "std": float(np.std(valid)),
        "min": float(np.min(valid)),
        "p5": float(np.percentile(valid, 5)),
        "p25": float(np.percentile(valid, 25)),
        "p50": float(np.percentile(valid, 50)),
        "p75": float(np.percentile(valid, 75)),
        "p95": float(np.percentile(valid, 95)),
        "max": float(np.max(valid)),
    }


# ---------------------------------------------------------------------------
# Run alpha (single mode, two phases: warmup then calibrated)
# ---------------------------------------------------------------------------


def _run_alpha(
    data: np.ndarray,
    use_tick_volume: bool,
    bar_volume_target: int = 500,
    n_vpin_buckets: int = 50,
    threshold_elevated: float = 0.4,
    threshold_toxic: float = 0.7,
    warmup_bars: int = 60,
) -> dict[str, Any]:
    """Run VPIN alpha on real data with calibration after warmup."""
    n = len(data)
    mode_name = "tick_volume" if use_tick_volume else "depth_proxy"

    # --- Phase 1: warmup pass to collect VPIN history ---
    alpha_warmup = VpinRegimeSwitchAlpha(
        bar_volume_target=bar_volume_target,
        n_vpin_buckets=n_vpin_buckets,
        threshold_elevated=threshold_elevated,
        threshold_toxic=threshold_toxic,
        warmup_bars=warmup_bars,
        use_tick_volume=use_tick_volume,
    )

    warmup_fraction = 0.15
    warmup_end = int(n * warmup_fraction)
    vpin_history: list[float] = []

    t0 = time.monotonic()
    for i in range(warmup_end):
        row = data[i]
        price_scaled = int(float(row["mid_price"]) * 10000)
        ts = int(row["local_ts"])

        if use_tick_volume:
            # Real data has volume=0 for BidAsk events; use depth-change as
            # surrogate volume (|delta_bid| + |delta_ask|) minimum 1
            if i > 0:
                delta_bid = abs(int(row["bid_qty"]) - int(data[i - 1]["bid_qty"]))
                delta_ask = abs(int(row["ask_qty"]) - int(data[i - 1]["ask_qty"]))
                volume = max(1, delta_bid + delta_ask)
            else:
                volume = 1
            alpha_warmup.update(price=price_scaled, volume=volume, ts=ts)
        else:
            mid_price_x2 = int(row["bid_px"] * 10000) + int(row["ask_px"] * 10000)
            bid_depth = max(1, int(row["bid_qty"]))
            ask_depth = max(1, int(row["ask_qty"]))
            alpha_warmup.update(mid_price_x2=mid_price_x2, bid_depth=bid_depth, ask_depth=ask_depth, ts=ts)

        if alpha_warmup.raw_vpin > 0.0:
            vpin_history.append(alpha_warmup.raw_vpin)

    warmup_time = time.monotonic() - t0
    logger.info(
        "warmup_complete",
        mode=mode_name,
        warmup_ticks=warmup_end,
        vpin_history_len=len(vpin_history),
        bars_seen=alpha_warmup.bars_seen,
        elapsed_s=round(warmup_time, 2),
    )

    # --- Phase 2: calibrate thresholds from warmup VPIN ---
    calibrated_elevated = threshold_elevated
    calibrated_toxic = threshold_toxic
    calibration_applied = False

    if len(vpin_history) >= 20:
        cal_detector = RegimeDetector(
            threshold_elevated=threshold_elevated,
            threshold_toxic=threshold_toxic,
        )
        cal_detector.calibrate(vpin_history)
        calibrated_elevated = cal_detector._threshold_elevated
        calibrated_toxic = cal_detector._threshold_toxic
        calibration_applied = True
        logger.info(
            "calibration_done",
            mode=mode_name,
            elevated=round(calibrated_elevated, 6),
            toxic=round(calibrated_toxic, 6),
        )

    # --- Phase 3: full pass with calibrated thresholds ---
    alpha = VpinRegimeSwitchAlpha(
        bar_volume_target=bar_volume_target,
        n_vpin_buckets=n_vpin_buckets,
        threshold_elevated=calibrated_elevated,
        threshold_toxic=calibrated_toxic,
        warmup_bars=warmup_bars,
        use_tick_volume=use_tick_volume,
    )

    vpin_series: list[float] = []
    regime_series: list[int] = []
    signal_series: list[float] = []
    mid_prices: list[float] = []

    t0 = time.monotonic()
    for i in range(n):
        row = data[i]
        mid_price = float(row["mid_price"])
        mid_prices.append(mid_price)
        price_scaled = int(mid_price * 10000)
        ts = int(row["local_ts"])

        if use_tick_volume:
            if i > 0:
                delta_bid = abs(int(row["bid_qty"]) - int(data[i - 1]["bid_qty"]))
                delta_ask = abs(int(row["ask_qty"]) - int(data[i - 1]["ask_qty"]))
                volume = max(1, delta_bid + delta_ask)
            else:
                volume = 1
            signal = alpha.update(price=price_scaled, volume=volume, ts=ts)
        else:
            mid_price_x2 = int(row["bid_px"] * 10000) + int(row["ask_px"] * 10000)
            bid_depth = max(1, int(row["bid_qty"]))
            ask_depth = max(1, int(row["ask_qty"]))
            signal = alpha.update(mid_price_x2=mid_price_x2, bid_depth=bid_depth, ask_depth=ask_depth, ts=ts)

        vpin_series.append(alpha.raw_vpin)
        regime_series.append(int(alpha.regime))
        signal_series.append(signal)

    run_time = time.monotonic() - t0
    logger.info("full_pass_done", mode=mode_name, elapsed_s=round(run_time, 2))

    vpin_arr = np.array(vpin_series, dtype=np.float64)
    regime_arr = np.array(regime_series, dtype=np.int32)
    signal_arr = np.array(signal_series, dtype=np.float64)
    mid_arr = np.array(mid_prices, dtype=np.float64)

    # --- Log returns ---
    log_returns = np.zeros(n, dtype=np.float64)
    log_returns[1:] = np.log(mid_arr[1:] / np.maximum(mid_arr[:-1], 1e-12))
    abs_returns = np.abs(log_returns)

    # --- C1: VPIN vs |returns| correlation ---
    post = slice(warmup_end, n)
    vpin_post = vpin_arr[post]
    abs_ret_post = abs_returns[post]
    c1_correlation = _correlation(vpin_post, abs_ret_post)

    # --- Regime-conditional volatility ---
    fwd_window = 50
    regime_vol: dict[str, list[float]] = {"LOW": [], "ELEVATED": [], "TOXIC": []}
    regime_names = {0: "LOW", 1: "ELEVATED", 2: "TOXIC"}
    for i in range(warmup_end, n - fwd_window):
        rname = regime_names[regime_arr[i]]
        fwd_vol = float(abs_returns[i : i + fwd_window].std())
        regime_vol[rname].append(fwd_vol)

    regime_vol_means: dict[str, float] = {}
    for rname, vals in regime_vol.items():
        regime_vol_means[rname] = float(np.mean(vals)) if vals else 0.0

    toxic_gt_elevated = regime_vol_means.get("TOXIC", 0) > regime_vol_means.get("ELEVATED", 0)
    elevated_gt_low = regime_vol_means.get("ELEVATED", 0) > regime_vol_means.get("LOW", 0)
    regime_ordering_correct = toxic_gt_elevated and elevated_gt_low

    # --- Regime distribution ---
    regime_counts = {
        "LOW": int(np.sum(regime_arr == 0)),
        "ELEVATED": int(np.sum(regime_arr == 1)),
        "TOXIC": int(np.sum(regime_arr == 2)),
    }
    regime_pct = {k: round(v / n * 100, 2) for k, v in regime_counts.items()}

    # --- Strategy PnL ---
    submit_delay_ticks = max(1, int(LATENCY_PROFILE["submit_ms"] / 2.0))
    position = 0.0
    pnl_series: list[float] = []
    cumulative_pnl = 0.0

    for i in range(warmup_end + submit_delay_ticks, n):
        delayed_signal = signal_arr[i - submit_delay_ticks]
        target = delayed_signal
        delta = target - position
        step = max(-0.1, min(0.1, delta))
        position += step
        tick_pnl = position * log_returns[i]
        cumulative_pnl += tick_pnl
        pnl_series.append(tick_pnl)

    pnl_arr = np.array(pnl_series, dtype=np.float64) if pnl_series else np.zeros(1)
    strategy_sharpe = _rolling_sharpe(pnl_arr)

    # --- VPIN stats ---
    vpin_nonzero = vpin_arr[vpin_arr > 0.0]
    vpin_stats = _percentiles(vpin_nonzero) if len(vpin_nonzero) > 0 else {}

    return {
        "mode": mode_name,
        "n_ticks": n,
        "bars_completed": alpha.bars_seen,
        "calibration_applied": calibration_applied,
        "calibrated_thresholds": {
            "elevated": round(calibrated_elevated, 6),
            "toxic": round(calibrated_toxic, 6),
        },
        "vpin_statistics": vpin_stats,
        "c1_vpin_abs_returns_correlation": round(c1_correlation, 6),
        "c1_pass": abs(c1_correlation) < 0.85,
        "regime_distribution": regime_counts,
        "regime_distribution_pct": regime_pct,
        "regime_vol_means": {k: round(v, 8) for k, v in regime_vol_means.items()},
        "regime_ordering_correct": regime_ordering_correct,
        "strategy_sharpe_annualised": round(strategy_sharpe, 4),
        "cumulative_pnl_log_returns": round(cumulative_pnl, 8),
        "elapsed_s": round(warmup_time + run_time, 2),
        "params": {
            "bar_volume_target": bar_volume_target,
            "n_vpin_buckets": n_vpin_buckets,
            "threshold_elevated_initial": threshold_elevated,
            "threshold_toxic_initial": threshold_toxic,
            "warmup_bars": warmup_bars,
        },
    }


# ---------------------------------------------------------------------------
# Parameter sensitivity sweep
# ---------------------------------------------------------------------------


def _param_sweep(data: np.ndarray) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    sweep_configs = [
        {"bar_volume_target": 100, "n_vpin_buckets": 30},
        {"bar_volume_target": 200, "n_vpin_buckets": 30},
        {"bar_volume_target": 500, "n_vpin_buckets": 50},
        {"bar_volume_target": 1000, "n_vpin_buckets": 50},
        {"bar_volume_target": 500, "n_vpin_buckets": 20},
        {"bar_volume_target": 500, "n_vpin_buckets": 50, "threshold_elevated": 0.3, "threshold_toxic": 0.6},
        {"bar_volume_target": 500, "n_vpin_buckets": 50, "threshold_elevated": 0.5, "threshold_toxic": 0.8},
    ]

    for cfg in sweep_configs:
        # Use depth_proxy for sweep (faster, real data has no trade volume)
        run = _run_alpha(
            data,
            use_tick_volume=False,
            bar_volume_target=cfg.get("bar_volume_target", 500),
            n_vpin_buckets=cfg.get("n_vpin_buckets", 50),
            threshold_elevated=cfg.get("threshold_elevated", 0.4),
            threshold_toxic=cfg.get("threshold_toxic", 0.7),
        )
        entry = {
            "params": cfg,
            "c1_correlation": run["c1_vpin_abs_returns_correlation"],
            "c1_pass": run["c1_pass"],
            "strategy_sharpe": run["strategy_sharpe_annualised"],
            "bars_completed": run["bars_completed"],
            "regime_ordering_correct": run["regime_ordering_correct"],
            "regime_distribution_pct": run["regime_distribution_pct"],
            "calibrated_thresholds": run["calibrated_thresholds"],
        }
        results.append(entry)
        logger.info(
            "param_sweep_run",
            params=cfg,
            c1=run["c1_vpin_abs_returns_correlation"],
            sharpe=run["strategy_sharpe_annualised"],
            regime_pct=run["regime_distribution_pct"],
        )

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="VPIN real-data backtest")
    parser.add_argument("--data", type=str, default=str(_DEFAULT_DATA), help="Path to .npy L1 data")
    args = parser.parse_args()

    data_path = Path(args.data)
    logger.info("vpin_real_backtest_start", data_path=str(data_path))

    if not data_path.exists():
        logger.error("data_not_found", path=str(data_path))
        return 1

    data = np.load(data_path, allow_pickle=True)
    n_rows = len(data)
    logger.info("data_loaded", rows=n_rows, fields=list(data.dtype.names or []))

    # Data validation
    ts_ns = data["local_ts"]
    vol_nonzero = int(np.sum(data["volume"] > 0))
    mid_prices = data["mid_price"]
    price_changes = int(np.sum(np.diff(mid_prices) != 0))

    print(f"\n{'='*70}")
    print(f"  VPIN Regime Switch — Real Data Backtest (TXFD6)")
    print(f"{'='*70}")
    print(f"  Data: {data_path.name}")
    print(f"  Rows: {n_rows:,}")
    print(f"  Price range: {mid_prices.min():.1f} - {mid_prices.max():.1f}")
    print(f"  Price changes: {price_changes:,} ({price_changes/n_rows*100:.1f}%)")
    print(f"  Volume > 0: {vol_nonzero:,} (using depth-churn proxy for tick_volume mode)")
    print(f"{'='*70}\n")

    # --- Run tick-volume mode (depth-churn as volume surrogate) ---
    print("[1/4] Running tick_volume mode (depth-churn as volume proxy)...")
    tick_result = _run_alpha(data, use_tick_volume=True)
    _print_mode_results("TICK_VOLUME", tick_result)

    # --- Run depth-proxy mode ---
    print("\n[2/4] Running depth_proxy mode...")
    depth_result = _run_alpha(data, use_tick_volume=False)
    _print_mode_results("DEPTH_PROXY", depth_result)

    # --- Cross-mode VPIN correlation ---
    print("\n[3/4] Computing cross-mode VPIN correlation...")
    tick_alpha = VpinRegimeSwitchAlpha(use_tick_volume=True)
    depth_alpha = VpinRegimeSwitchAlpha(use_tick_volume=False)
    tick_vpins: list[float] = []
    depth_vpins: list[float] = []

    for i in range(n_rows):
        row = data[i]
        price_scaled = int(float(row["mid_price"]) * 10000)
        ts = int(row["local_ts"])
        mid_price_x2 = int(row["bid_px"] * 10000) + int(row["ask_px"] * 10000)
        bid_depth = max(1, int(row["bid_qty"]))
        ask_depth = max(1, int(row["ask_qty"]))

        if i > 0:
            delta_bid = abs(int(row["bid_qty"]) - int(data[i - 1]["bid_qty"]))
            delta_ask = abs(int(row["ask_qty"]) - int(data[i - 1]["ask_qty"]))
            volume = max(1, delta_bid + delta_ask)
        else:
            volume = 1

        tick_alpha.update(price=price_scaled, volume=volume, ts=ts)
        depth_alpha.update(mid_price_x2=mid_price_x2, bid_depth=bid_depth, ask_depth=ask_depth, ts=ts)
        tick_vpins.append(tick_alpha.raw_vpin)
        depth_vpins.append(depth_alpha.raw_vpin)

    warmup_end = int(n_rows * 0.15)
    cross_mode_corr = _correlation(
        np.array(tick_vpins[warmup_end:]),
        np.array(depth_vpins[warmup_end:]),
    )
    print(f"  Cross-mode VPIN correlation: {cross_mode_corr:.6f}")

    # --- Parameter sweep ---
    print("\n[4/4] Parameter sensitivity sweep (depth_proxy mode)...")
    sweep_results = _param_sweep(data)

    # --- Assemble output ---
    output: dict[str, Any] = {
        "stage": "4_real_data_backtest",
        "alpha_id": "vpin_regime_switch",
        "data_source": str(data_path),
        "data_rows": n_rows,
        "data_note": "Real TXFD6 L1 data from ClickHouse. volume=0 for all rows (BidAsk only). "
                     "tick_volume mode uses depth-churn as volume surrogate.",
        "tick_volume_results": tick_result,
        "depth_proxy_results": depth_result,
        "cross_mode_vpin_correlation": round(cross_mode_corr, 6),
        "parameter_sensitivity": sweep_results,
        "challenger_conditions": {
            "C1_vpin_abs_returns_corr_lt_085": {
                "tick_volume": tick_result["c1_pass"],
                "depth_proxy": depth_result["c1_pass"],
                "values": {
                    "tick_volume": tick_result["c1_vpin_abs_returns_correlation"],
                    "depth_proxy": depth_result["c1_vpin_abs_returns_correlation"],
                },
            },
        },
        "key_findings": {
            "calibrate_fixes_degenerate": None,  # filled below
            "regime_ordering_correct": {
                "tick_volume": tick_result["regime_ordering_correct"],
                "depth_proxy": depth_result["regime_ordering_correct"],
            },
            "strategy_sharpe": {
                "tick_volume": tick_result["strategy_sharpe_annualised"],
                "depth_proxy": depth_result["strategy_sharpe_annualised"],
            },
        },
        "latency_profile_applied": LATENCY_PROFILE,
    }

    # Check if calibration fixed degenerate distribution
    for mode_key, result in [("tick_volume", tick_result), ("depth_proxy", depth_result)]:
        dist = result["regime_distribution_pct"]
        is_degenerate = any(v > 98.0 for v in dist.values())
        output["key_findings"]["calibrate_fixes_degenerate"] = not is_degenerate

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    logger.info("results_saved", path=str(_OUT_PATH))

    # --- Final summary ---
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  C1 (VPIN vs |ret|):")
    print(f"    tick_volume: {tick_result['c1_vpin_abs_returns_correlation']:.6f}  PASS={tick_result['c1_pass']}")
    print(f"    depth_proxy: {depth_result['c1_vpin_abs_returns_correlation']:.6f}  PASS={depth_result['c1_pass']}")
    print(f"  Regime ordering (TOXIC > ELEVATED > LOW vol):")
    print(f"    tick_volume: {tick_result['regime_ordering_correct']}")
    print(f"    depth_proxy: {depth_result['regime_ordering_correct']}")
    print(f"  Strategy Sharpe (annualised):")
    print(f"    tick_volume: {tick_result['strategy_sharpe_annualised']:.4f}")
    print(f"    depth_proxy: {depth_result['strategy_sharpe_annualised']:.4f}")
    print(f"  Cross-mode VPIN correlation: {cross_mode_corr:.6f}")
    print(f"  Calibrated thresholds:")
    print(f"    tick_volume: elevated={tick_result['calibrated_thresholds']['elevated']:.6f}, toxic={tick_result['calibrated_thresholds']['toxic']:.6f}")
    print(f"    depth_proxy: elevated={depth_result['calibrated_thresholds']['elevated']:.6f}, toxic={depth_result['calibrated_thresholds']['toxic']:.6f}")
    print(f"  Regime distribution (after calibration):")
    print(f"    tick_volume: {tick_result['regime_distribution_pct']}")
    print(f"    depth_proxy: {depth_result['regime_distribution_pct']}")
    print(f"  Output: {_OUT_PATH}")
    print(f"{'='*70}\n")

    return 0


def _print_mode_results(mode: str, result: dict[str, Any]) -> None:
    print(f"\n  --- {mode} ---")
    print(f"  Bars completed: {result['bars_completed']:,}")
    print(f"  Calibration applied: {result['calibration_applied']}")
    print(f"  Calibrated thresholds: elevated={result['calibrated_thresholds']['elevated']:.6f}, toxic={result['calibrated_thresholds']['toxic']:.6f}")
    print(f"  VPIN stats: {_fmt_stats(result.get('vpin_statistics', {}))}")
    print(f"  C1 (VPIN vs |ret|): {result['c1_vpin_abs_returns_correlation']:.6f}  PASS={result['c1_pass']}")
    print(f"  Regime distribution: {result['regime_distribution_pct']}")
    print(f"  Regime vol means: {result['regime_vol_means']}")
    print(f"  Regime ordering correct: {result['regime_ordering_correct']}")
    print(f"  Strategy Sharpe: {result['strategy_sharpe_annualised']:.4f}")
    print(f"  Cumulative PnL (log ret): {result['cumulative_pnl_log_returns']:.8f}")
    print(f"  Elapsed: {result['elapsed_s']:.1f}s")


def _fmt_stats(stats: dict[str, float]) -> str:
    if not stats:
        return "N/A"
    return (
        f"mean={stats.get('mean', 0):.4f}, "
        f"std={stats.get('std', 0):.4f}, "
        f"p5={stats.get('p5', 0):.4f}, "
        f"p50={stats.get('p50', 0):.4f}, "
        f"p95={stats.get('p95', 0):.4f}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
