"""Signal replay backtest for L5 alphas (mlofi_gradient, book_convexity, ofi_depth_divergence).

Replays L5 bid/ask data through alpha signal generators, computes forward
mid-price returns at multiple horizons, and calculates information coefficient
(IC = rank correlation between signal and forward return).

Handles day boundaries, per-day IC aggregation, cross-alpha correlation.

Usage:
    uv run python -m research.tools.signal_replay_l5 \
        --symbols 2330,2317,TXFD6 \
        --data-dir research/data/l5/ \
        --out outputs/team_artifacts/alpha-research/
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from scipy import stats as scipy_stats

_HERE = Path(__file__).resolve()
_RESEARCH_ROOT = _HERE.parent.parent
_PROJECT_ROOT = _RESEARCH_ROOT.parent
for _p in (_PROJECT_ROOT, _RESEARCH_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from research.alphas.book_convexity.impl import BookConvexityAlpha
from research.alphas.mlofi_gradient.impl import MlofiGradientAlpha
from research.alphas.ofi_depth_divergence.impl import OfiDepthDivergenceAlpha

logger = structlog.get_logger(__name__)

# Forward return horizons
_HORIZONS_MS = [100, 500, 1_000, 5_000, 30_000]
_HORIZONS_NS = [h * 1_000_000 for h in _HORIZONS_MS]
_HORIZON_LABELS = ["100ms", "500ms", "1s", "5s", "30s"]

# Latency: Shioaji P95 submit RTT
_LATENCY_NS = 36_000_000  # 36ms

# Warmup ticks per alpha
_WARMUP = {"mlofi_gradient": 64, "book_convexity": 32, "ofi_depth_divergence": 64}

# 1 day in nanoseconds (for day boundary detection)
_NS_PER_DAY = 86_400_000_000_000
# TWSE trading hours: 09:00 - 13:30 TWD (UTC+8) => 01:00 - 05:30 UTC
# Gap > 4 hours between ticks = day boundary
_DAY_GAP_NS = 4 * 3_600_000_000_000


def _split_days(timestamps: np.ndarray) -> list[tuple[int, int]]:
    """Split timestamp array into per-day segments by detecting gaps > 4h."""
    if len(timestamps) == 0:
        return []
    gaps = np.diff(timestamps)
    boundaries = np.where(gaps > _DAY_GAP_NS)[0] + 1
    starts = np.concatenate([[0], boundaries])
    ends = np.concatenate([boundaries, [len(timestamps)]])
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def _compute_forward_returns(
    timestamps: np.ndarray,
    mid_prices: np.ndarray,
    day_segments: list[tuple[int, int]],
) -> dict[str, np.ndarray]:
    """Compute forward mid-price returns, respecting day boundaries."""
    n = len(timestamps)
    results: dict[str, np.ndarray] = {}

    for horizon_ns, label in zip(_HORIZONS_NS, _HORIZON_LABELS):
        fwd_ret = np.full(n, np.nan, dtype=np.float64)
        target_offset = _LATENCY_NS + horizon_ns

        for day_start, day_end in day_segments:
            j = day_start
            for i in range(day_start, day_end):
                target_ts = timestamps[i] + target_offset
                while j < day_end and timestamps[j] < target_ts:
                    j += 1
                if j < day_end and mid_prices[i] > 0:
                    fwd_ret[i] = (mid_prices[j] - mid_prices[i]) / mid_prices[i]
                # j stays within same day segment

        results[label] = fwd_ret

    return results


def _replay_alpha(alpha: Any, data: np.ndarray) -> np.ndarray:
    """Replay L5 data through an alpha and collect per-tick signals."""
    n = len(data)
    signals = np.empty(n, dtype=np.float64)

    for i in range(n):
        bids = np.column_stack([data[i]["bids_price"], data[i]["bids_vol"]]).astype(np.float64)
        asks = np.column_stack([data[i]["asks_price"], data[i]["asks_vol"]]).astype(np.float64)
        signals[i] = alpha.update(bids=bids, asks=asks)

    return signals


def _rank_ic(signals: np.ndarray, returns: np.ndarray) -> float:
    """Spearman rank IC."""
    valid = np.isfinite(signals) & np.isfinite(returns) & (signals != 0.0)
    if valid.sum() < 30:
        return float("nan")
    corr, _ = scipy_stats.spearmanr(signals[valid], returns[valid])
    return float(corr)


def _pearson_ic(signals: np.ndarray, returns: np.ndarray) -> float:
    """Pearson IC."""
    valid = np.isfinite(signals) & np.isfinite(returns) & (signals != 0.0)
    if valid.sum() < 30:
        return float("nan")
    corr, _ = scipy_stats.pearsonr(signals[valid], returns[valid])
    return float(corr)


def _ic_tstat(ic: float, n: int) -> float:
    """IC t-statistic: IC * sqrt(N) / sqrt(1 - IC^2)."""
    if np.isnan(ic) or abs(ic) >= 1.0 or n < 2:
        return float("nan")
    return ic * np.sqrt(n) / np.sqrt(1 - ic * ic)


def _per_day_ic(
    signals: np.ndarray,
    fwd_returns: dict[str, np.ndarray],
    day_segments: list[tuple[int, int]],
    warmup: int,
) -> dict[str, dict[str, float]]:
    """Compute IC per day, then aggregate mean and std across days."""
    result: dict[str, dict[str, float]] = {}

    for label in _HORIZON_LABELS:
        daily_ics: list[float] = []
        for day_start, day_end in day_segments:
            # Skip warmup within each day
            eff_start = day_start + warmup
            if eff_start >= day_end:
                continue
            s = signals[eff_start:day_end]
            r = fwd_returns[label][eff_start:day_end]
            ic = _rank_ic(s, r)
            if not np.isnan(ic):
                daily_ics.append(ic)

        if daily_ics:
            result[label] = {
                "ic_mean": float(np.mean(daily_ics)),
                "ic_std": float(np.std(daily_ics)),
                "ic_sharpe": float(np.mean(daily_ics) / np.std(daily_ics)) if np.std(daily_ics) > 0 else float("nan"),
                "n_days": len(daily_ics),
            }
        else:
            result[label] = {"ic_mean": float("nan"), "ic_std": float("nan"), "ic_sharpe": float("nan"), "n_days": 0}

    return result


def _signal_stats(signals: np.ndarray, warmup: int) -> dict[str, float]:
    """Signal distribution statistics (excluding warmup)."""
    s = signals[warmup:]
    nonzero = s[s != 0.0]
    if len(nonzero) == 0:
        return {"mean": 0.0, "std": 0.0, "skew": 0.0, "kurtosis": 0.0, "min": 0.0, "max": 0.0, "pct_nonzero": 0.0, "autocorr_lag1": 0.0, "turnover": 0.0}

    # Autocorrelation lag-1
    if len(s) > 1:
        autocorr = float(np.corrcoef(s[:-1], s[1:])[0, 1]) if np.std(s) > 0 else 0.0
    else:
        autocorr = 0.0

    # Turnover: mean absolute change per tick
    turnover = float(np.mean(np.abs(np.diff(s)))) if len(s) > 1 else 0.0

    return {
        "mean": float(np.mean(nonzero)),
        "std": float(np.std(nonzero)),
        "skew": float(scipy_stats.skew(nonzero)),
        "kurtosis": float(scipy_stats.kurtosis(nonzero)),
        "min": float(np.min(nonzero)),
        "max": float(np.max(nonzero)),
        "pct_nonzero": float(len(nonzero) / len(s) * 100),
        "autocorr_lag1": autocorr,
        "turnover": turnover,
    }


def run_backtest(
    symbols: list[str],
    data_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Run full signal replay backtest."""
    output_dir.mkdir(parents=True, exist_ok=True)

    alpha_factories: dict[str, type] = {
        "mlofi_gradient": MlofiGradientAlpha,
        "book_convexity": BookConvexityAlpha,
        "ofi_depth_divergence": OfiDepthDivergenceAlpha,
    }

    all_results: list[dict[str, Any]] = []
    # Store signals for cross-alpha correlation
    signal_store: dict[str, dict[str, np.ndarray]] = {}  # {symbol: {alpha: signals}}

    for sym in symbols:
        npy_path = data_dir / f"{sym}_l5.npy"
        if not npy_path.exists():
            logger.warning("data_not_found", symbol=sym)
            continue

        data = np.load(str(npy_path))
        log = logger.bind(symbol=sym, n_rows=len(data))

        # Compute mid prices and day segments once per symbol
        bp_l1 = data["bids_price"][:, 0].astype(np.float64)
        ap_l1 = data["asks_price"][:, 0].astype(np.float64)
        mid = (bp_l1 + ap_l1) / 2.0
        timestamps = data["timestamp_ns"]
        day_segments = _split_days(timestamps)
        log.info("days_detected", n_days=len(day_segments))

        # Compute forward returns once per symbol
        fwd_returns = _compute_forward_returns(timestamps, mid, day_segments)

        signal_store[sym] = {}

        for alpha_name, alpha_cls in alpha_factories.items():
            warmup = _WARMUP[alpha_name]
            log_a = log.bind(alpha=alpha_name)
            log_a.info("replay_start")

            alpha = alpha_cls()
            t0 = time.perf_counter()
            signals = _replay_alpha(alpha, data)
            replay_time = time.perf_counter() - t0

            signal_store[sym][alpha_name] = signals

            # Overall IC (excluding warmup)
            ic_overall: dict[str, dict[str, float]] = {}
            for label in _HORIZON_LABELS:
                s = signals[warmup:]
                r = fwd_returns[label][warmup:]
                ric = _rank_ic(s, r)
                pic = _pearson_ic(s, r)
                n_valid = int(np.sum(np.isfinite(s) & np.isfinite(r) & (s != 0.0)))
                ic_overall[label] = {
                    "rank_ic": ric,
                    "pearson_ic": pic,
                    "t_stat": _ic_tstat(ric, n_valid),
                    "n_valid": n_valid,
                }

            # Per-day IC
            daily_ic = _per_day_ic(signals, fwd_returns, day_segments, warmup)

            # Signal stats
            stats = _signal_stats(signals, warmup)

            log_a.info(
                "replay_done",
                time_s=f"{replay_time:.1f}",
                ic_1s=f"{ic_overall['1s']['rank_ic']:.4f}",
            )

            all_results.append({
                "symbol": sym,
                "alpha": alpha_name,
                "n_rows": len(data),
                "n_days": len(day_segments),
                "ic_overall": ic_overall,
                "ic_daily": daily_ic,
                "signal_stats": stats,
                "replay_time_s": round(replay_time, 2),
            })

    # Cross-alpha correlation
    cross_corr: dict[str, dict[str, float]] = {}
    alpha_names = list(alpha_factories.keys())
    for sym in symbols:
        if sym not in signal_store:
            continue
        sym_corr: dict[str, float] = {}
        for i, a1 in enumerate(alpha_names):
            for a2 in alpha_names[i + 1:]:
                if a1 in signal_store[sym] and a2 in signal_store[sym]:
                    s1 = signal_store[sym][a1]
                    s2 = signal_store[sym][a2]
                    valid = (s1 != 0.0) & (s2 != 0.0)
                    if valid.sum() > 30:
                        c, _ = scipy_stats.pearsonr(s1[valid], s2[valid])
                        sym_corr[f"{a1}_vs_{a2}"] = round(float(c), 4)
        cross_corr[sym] = sym_corr

    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "latency_profile": "shioaji_sim_p95_v2026-03-04",
        "latency_applied_ns": _LATENCY_NS,
        "horizons_ms": _HORIZONS_MS,
        "symbols": symbols,
        "results": all_results,
        "cross_alpha_correlation": cross_corr,
    }

    # Save JSON
    json_path = output_dir / "stage4_backtest_data.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("json_saved", path=str(json_path))

    # Print summary table
    _print_summary(report, alpha_names)

    return report


def _print_summary(report: dict[str, Any], alpha_names: list[str]) -> None:
    print("\n" + "=" * 80)
    print("SIGNAL REPLAY BACKTEST RESULTS")
    print(f"Latency: {_LATENCY_NS / 1e6:.0f}ms (Shioaji P95), Horizons: {_HORIZON_LABELS}")
    print("=" * 80)

    for aname in alpha_names:
        print(f"\n--- {aname} ---")
        print(f"{'Symbol':>8s} {'Rows':>10s}  ", end="")
        for label in _HORIZON_LABELS:
            print(f"{'IC_' + label:>10s}", end="")
        print(f"  {'AutoCorr':>8s} {'Turnover':>8s}")

        aresults = [r for r in report["results"] if r["alpha"] == aname]
        for r in aresults:
            print(f"{r['symbol']:>8s} {r['n_rows']:>10,d}  ", end="")
            for label in _HORIZON_LABELS:
                ic = r["ic_overall"][label]["rank_ic"]
                tstat = r["ic_overall"][label]["t_stat"]
                sig = "*" if abs(tstat) > 2.0 else " "
                print(f"{ic:>+9.4f}{sig}", end="")
            print(f"  {r['signal_stats']['autocorr_lag1']:>8.4f} {r['signal_stats']['turnover']:>8.6f}")

        # Daily IC stability
        print(f"\n  Daily IC mean +/- std:")
        for r in aresults:
            print(f"  {r['symbol']:>8s}: ", end="")
            for label in _HORIZON_LABELS:
                d = r["ic_daily"][label]
                print(f"{label}={d['ic_mean']:+.4f}+/-{d['ic_std']:.4f}  ", end="")
            print()

    # Cross-alpha correlation
    if report.get("cross_alpha_correlation"):
        print(f"\n--- Cross-Alpha Correlation ---")
        for sym, corrs in report["cross_alpha_correlation"].items():
            for pair, c in corrs.items():
                print(f"  {sym:>8s}: {pair} = {c:+.4f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Signal replay backtest for L5 alphas.")
    parser.add_argument("--symbols", default="2330,2317,TXFD6")
    parser.add_argument("--data-dir", default="research/data/l5/")
    parser.add_argument("--out", default="outputs/team_artifacts/alpha-research/")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    run_backtest(symbols=symbols, data_dir=Path(args.data_dir), output_dir=Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
