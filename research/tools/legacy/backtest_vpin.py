"""VPIN Regime Switch Alpha — Stage 4 standalone backtest.

Loads synthetic LOB data, feeds it through VpinRegimeSwitchAlpha in both
tick-volume and depth-proxy modes, and validates challenger conditions.

Outputs: outputs/team_artifacts/alpha-research/stage4_vpin_backtest.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import structlog

# ---------------------------------------------------------------------------
# Path setup — ensure repo root is on sys.path for research.* imports
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from research.alphas.vpin_regime_switch.impl import (  # noqa: E402
    Regime,
    VpinRegimeSwitchAlpha,
)

logger = structlog.get_logger("backtest.vpin")

# ---------------------------------------------------------------------------
# Latency profile: shioaji_sim_p95_v2026-03-04
# ---------------------------------------------------------------------------

LATENCY_PROFILE = {
    "name": "shioaji_sim_p95_v2026-03-04",
    "submit_ms": 36.0,
    "modify_ms": 43.0,
    "cancel_ms": 47.0,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATA_PATH = _REPO_ROOT / "research" / "data" / "processed" / "vpin_regime_switch" / "synthetic_lob_v2_train.npy"
_OUT_PATH = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "stage4_vpin_backtest.json"


def _autocorrelation(series: np.ndarray, lag: int = 1) -> float:
    """Compute autocorrelation at given lag."""
    if len(series) < lag + 2:
        return 0.0
    x = series[:-lag]
    y = series[lag:]
    mx, my = x.mean(), y.mean()
    sx, sy = x.std(), y.std()
    if sx < 1e-15 or sy < 1e-15:
        return 0.0
    return float(np.mean((x - mx) * (y - my)) / (sx * sy))


def _rolling_sharpe(returns: np.ndarray, window: int = 200) -> float:
    """Compute annualised rolling Sharpe (simple mean of rolling windows)."""
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


def _correlation(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson correlation between two arrays."""
    if len(x) < 3 or len(y) < 3:
        return 0.0
    sx, sy = x.std(), y.std()
    if sx < 1e-15 or sy < 1e-15:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


# ---------------------------------------------------------------------------
# Run alpha on data in a given mode
# ---------------------------------------------------------------------------

def _run_alpha(
    data: np.ndarray,
    use_tick_volume: bool,
    bar_volume_target: int = 500,
    n_vpin_buckets: int = 50,
    threshold_elevated: float = 0.4,
    threshold_toxic: float = 0.7,
) -> dict[str, Any]:
    """Run VPIN alpha and return results dict."""
    alpha = VpinRegimeSwitchAlpha(
        bar_volume_target=bar_volume_target,
        n_vpin_buckets=n_vpin_buckets,
        threshold_elevated=threshold_elevated,
        threshold_toxic=threshold_toxic,
        use_tick_volume=use_tick_volume,
    )

    n = len(data)
    vpin_series: list[float] = []
    regime_series: list[int] = []
    signal_series: list[float] = []
    mid_prices: list[float] = []

    for i in range(n):
        row = data[i]
        mid_price = float(row["mid_price"])
        mid_prices.append(mid_price)

        # Scale to int x10000 for platform convention
        price_scaled = int(mid_price * 10000)
        volume = max(1, int(row["volume"]))
        bid_depth = max(1, int(row["bid_qty"]))
        ask_depth = max(1, int(row["ask_qty"]))
        ts = int(row["local_ts"])

        mid_price_x2 = int(row["bid_px"] * 10000) + int(row["ask_px"] * 10000)

        if use_tick_volume:
            signal = alpha.update(price=price_scaled, volume=volume, ts=ts)
        else:
            signal = alpha.update(
                mid_price_x2=mid_price_x2,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                ts=ts,
            )

        vpin_series.append(alpha.raw_vpin)
        regime_series.append(int(alpha.regime))
        signal_series.append(signal)

    vpin_arr = np.array(vpin_series, dtype=np.float64)
    regime_arr = np.array(regime_series, dtype=np.int32)
    signal_arr = np.array(signal_series, dtype=np.float64)
    mid_arr = np.array(mid_prices, dtype=np.float64)

    # --- Compute log returns ---
    log_returns = np.zeros(n, dtype=np.float64)
    log_returns[1:] = np.log(mid_arr[1:] / np.maximum(mid_arr[:-1], 1e-12))
    abs_returns = np.abs(log_returns)

    # --- C1: VPIN vs |returns| correlation ---
    # Skip warmup period (first 15% of data)
    warmup_end = int(n * 0.15)
    vpin_post = vpin_arr[warmup_end:]
    abs_ret_post = abs_returns[warmup_end:]
    c1_correlation = _correlation(vpin_post, abs_ret_post)

    # --- Regime transition accuracy ---
    # Check: does TOXIC regime predict higher subsequent volatility?
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

    # Regime accuracy: TOXIC vol > ELEVATED vol > LOW vol
    toxic_gt_elevated = regime_vol_means.get("TOXIC", 0) > regime_vol_means.get("ELEVATED", 0)
    elevated_gt_low = regime_vol_means.get("ELEVATED", 0) > regime_vol_means.get("LOW", 0)
    regime_ordering_correct = toxic_gt_elevated and elevated_gt_low

    # --- Signal stability: autocorrelation of VPIN ---
    vpin_autocorr = _autocorrelation(vpin_arr[warmup_end:], lag=1)

    # --- Simple strategy PnL with latency ---
    # Reduce position in TOXIC, increase in LOW
    submit_delay_ticks = max(1, int(LATENCY_PROFILE["submit_ms"] / 2.0))  # 2ms per tick

    position = 0.0
    pnl_series: list[float] = []
    cumulative_pnl = 0.0

    for i in range(warmup_end + submit_delay_ticks, n):
        # Signal from submit_delay_ticks ago (latency-delayed)
        delayed_signal = signal_arr[i - submit_delay_ticks]

        # Target position: +1 for LOW, 0 for ELEVATED, -1 for TOXIC
        target = delayed_signal

        # Step towards target (capped at 0.1 per tick to model gradual adjustment)
        delta = target - position
        step = max(-0.1, min(0.1, delta))
        position += step

        # PnL from holding position * return
        tick_pnl = position * log_returns[i]
        cumulative_pnl += tick_pnl
        pnl_series.append(tick_pnl)

    pnl_arr = np.array(pnl_series, dtype=np.float64) if pnl_series else np.zeros(1)
    strategy_sharpe = _rolling_sharpe(pnl_arr)

    # --- Regime distribution ---
    regime_counts = {
        "LOW": int(np.sum(regime_arr == 0)),
        "ELEVATED": int(np.sum(regime_arr == 1)),
        "TOXIC": int(np.sum(regime_arr == 2)),
    }

    bars_completed = alpha.bars_seen

    return {
        "mode": "tick_volume" if use_tick_volume else "depth_proxy",
        "n_ticks": n,
        "bars_completed": bars_completed,
        "c1_vpin_abs_returns_correlation": round(c1_correlation, 6),
        "c1_pass": abs(c1_correlation) < 0.85,
        "regime_distribution": regime_counts,
        "regime_vol_means": {k: round(v, 8) for k, v in regime_vol_means.items()},
        "regime_ordering_correct": regime_ordering_correct,
        "vpin_autocorrelation_lag1": round(vpin_autocorr, 6),
        "strategy_sharpe_annualised": round(strategy_sharpe, 4),
        "cumulative_pnl_log_returns": round(cumulative_pnl, 8),
        "latency_profile": LATENCY_PROFILE,
        "params": {
            "bar_volume_target": bar_volume_target,
            "n_vpin_buckets": n_vpin_buckets,
            "threshold_elevated": threshold_elevated,
            "threshold_toxic": threshold_toxic,
        },
    }


# ---------------------------------------------------------------------------
# Parameter sensitivity sweep
# ---------------------------------------------------------------------------

def _param_sweep(data: np.ndarray) -> list[dict[str, Any]]:
    """Sweep key parameters and record C1 correlation + sharpe."""
    results: list[dict[str, Any]] = []
    sweep_configs = [
        {"bar_volume_target": 200, "n_vpin_buckets": 30},
        {"bar_volume_target": 500, "n_vpin_buckets": 50},
        {"bar_volume_target": 1000, "n_vpin_buckets": 50},
        {"bar_volume_target": 500, "n_vpin_buckets": 20},
        {"bar_volume_target": 500, "n_vpin_buckets": 50, "threshold_elevated": 0.3, "threshold_toxic": 0.6},
        {"bar_volume_target": 500, "n_vpin_buckets": 50, "threshold_elevated": 0.5, "threshold_toxic": 0.8},
    ]

    for cfg in sweep_configs:
        run = _run_alpha(
            data,
            use_tick_volume=True,
            bar_volume_target=cfg.get("bar_volume_target", 500),
            n_vpin_buckets=cfg.get("n_vpin_buckets", 50),
            threshold_elevated=cfg.get("threshold_elevated", 0.4),
            threshold_toxic=cfg.get("threshold_toxic", 0.7),
        )
        results.append({
            "params": cfg,
            "c1_correlation": run["c1_vpin_abs_returns_correlation"],
            "c1_pass": run["c1_pass"],
            "strategy_sharpe": run["strategy_sharpe_annualised"],
            "bars_completed": run["bars_completed"],
            "regime_ordering_correct": run["regime_ordering_correct"],
        })
        logger.info(
            "param_sweep_run",
            params=cfg,
            c1=run["c1_vpin_abs_returns_correlation"],
            sharpe=run["strategy_sharpe_annualised"],
        )

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logger.info("vpin_backtest_start", data_path=str(_DATA_PATH))

    if not _DATA_PATH.exists():
        logger.error("data_not_found", path=str(_DATA_PATH))
        return 1

    data = np.load(_DATA_PATH, allow_pickle=True)
    logger.info("data_loaded", rows=len(data), fields=list(data.dtype.names or []))

    # Validate data
    n_rows = len(data)
    volume_nonzero = int(np.sum(data["volume"] > 0))
    ts_ns = data["local_ts"]
    assert n_rows >= 20000, f"Insufficient rows: {n_rows}"
    assert volume_nonzero > n_rows * 0.9, f"Too many zero-volume rows: {n_rows - volume_nonzero}"
    assert ts_ns[-1] > ts_ns[0], "local_ts not increasing"
    logger.info(
        "data_validated",
        n_rows=n_rows,
        volume_nonzero=volume_nonzero,
        ts_range_ns=(int(ts_ns[0]), int(ts_ns[-1])),
    )

    # Run both modes
    logger.info("running_tick_volume_mode")
    tick_result = _run_alpha(data, use_tick_volume=True)
    logger.info(
        "tick_volume_done",
        c1=tick_result["c1_vpin_abs_returns_correlation"],
        sharpe=tick_result["strategy_sharpe_annualised"],
        bars=tick_result["bars_completed"],
    )

    logger.info("running_depth_proxy_mode")
    depth_result = _run_alpha(data, use_tick_volume=False)
    logger.info(
        "depth_proxy_done",
        c1=depth_result["c1_vpin_abs_returns_correlation"],
        sharpe=depth_result["strategy_sharpe_annualised"],
        bars=depth_result["bars_completed"],
    )

    # Cross-mode VPIN correlation
    # Re-run to get VPIN series for correlation comparison
    logger.info("computing_cross_mode_vpin_correlation")
    tick_alpha = VpinRegimeSwitchAlpha(use_tick_volume=True)
    depth_alpha = VpinRegimeSwitchAlpha(use_tick_volume=False)
    tick_vpins: list[float] = []
    depth_vpins: list[float] = []

    for i in range(n_rows):
        row = data[i]
        price_scaled = int(float(row["mid_price"]) * 10000)
        volume = max(1, int(row["volume"]))
        bid_depth = max(1, int(row["bid_qty"]))
        ask_depth = max(1, int(row["ask_qty"]))
        ts = int(row["local_ts"])
        mid_price_x2 = int(row["bid_px"] * 10000) + int(row["ask_px"] * 10000)

        tick_alpha.update(price=price_scaled, volume=volume, ts=ts)
        depth_alpha.update(mid_price_x2=mid_price_x2, bid_depth=bid_depth, ask_depth=ask_depth, ts=ts)

        tick_vpins.append(tick_alpha.raw_vpin)
        depth_vpins.append(depth_alpha.raw_vpin)

    warmup_end = int(n_rows * 0.15)
    cross_mode_corr = _correlation(
        np.array(tick_vpins[warmup_end:]),
        np.array(depth_vpins[warmup_end:]),
    )
    logger.info("cross_mode_correlation", correlation=round(cross_mode_corr, 6))

    # Parameter sensitivity sweep
    logger.info("starting_param_sweep")
    sweep_results = _param_sweep(data)

    # Assemble output
    output: dict[str, Any] = {
        "stage": "4_backtest",
        "alpha_id": "vpin_regime_switch",
        "data_source": str(_DATA_PATH),
        "data_rows": n_rows,
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
            "C2_bvc_accuracy": {
                "note": "BVC accuracy requires ground-truth trade labels not available in synthetic data. "
                        "Partial proxy: cross-mode correlation measures consistency between tick-rule and depth-churn.",
                "cross_mode_correlation": round(cross_mode_corr, 6),
            },
        },
        "latency_profile_applied": LATENCY_PROFILE,
    }

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    logger.info("results_saved", path=str(_OUT_PATH))

    # Summary
    c1_tick = tick_result["c1_vpin_abs_returns_correlation"]
    c1_depth = depth_result["c1_vpin_abs_returns_correlation"]
    all_c1_pass = tick_result["c1_pass"] and depth_result["c1_pass"]
    logger.info(
        "backtest_summary",
        c1_tick_vol=c1_tick,
        c1_depth_proxy=c1_depth,
        c1_all_pass=all_c1_pass,
        sharpe_tick=tick_result["strategy_sharpe_annualised"],
        sharpe_depth=depth_result["strategy_sharpe_annualised"],
        regime_ordering_tick=tick_result["regime_ordering_correct"],
        cross_mode_corr=round(cross_mode_corr, 6),
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
