"""Batch 3 evaluation: Depth Geometry alphas against TXFC6 tick data.

Alphas: depth_momentum, depth_velocity_diff, depth_ratio, depth_ratio_log, depth_shock

Usage:
    uv run python -m research.tools.eval_batch_3_depth
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats as sp_stats

# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent.parent  # hft_platform root
RESEARCH = ROOT / "research"

# Data may live in the main repo when running from a worktree
_MAIN_RESEARCH = Path("/home/charlie/hft_platform/research")
_DATA_ROOT = _MAIN_RESEARCH if (_MAIN_RESEARCH / "data" / "raw" / "txfc6").exists() else RESEARCH

# ---------------------------------------------------------------------------
# Alpha imports
# ---------------------------------------------------------------------------
from research.alphas.depth_momentum.impl import ALPHA_CLASS as DepthMomentumCls  # noqa: E402
from research.alphas.depth_velocity_diff.impl import ALPHA_CLASS as DepthVelocityDiffCls  # noqa: E402
from research.alphas.depth_ratio.impl import ALPHA_CLASS as DepthRatioCls  # noqa: E402
from research.alphas.depth_ratio_log.impl import ALPHA_CLASS as DepthRatioLogCls  # noqa: E402
from research.alphas.depth_shock.impl import ALPHA_CLASS as DepthShockCls  # noqa: E402

ALPHAS = {
    "depth_momentum": DepthMomentumCls,
    "depth_velocity_diff": DepthVelocityDiffCls,
    "depth_ratio": DepthRatioCls,
    "depth_ratio_log": DepthRatioLogCls,
    "depth_shock": DepthShockCls,
}

HORIZONS = (1, 5, 10, 20, 50)
OOS_RATIO = 0.3

# Data paths
PRIMARY_DATA = _DATA_ROOT / "data" / "raw" / "txfc6" / "TXFC6_all_l1.npy"
CROSS_VAL_DATA = {
    "2330": _DATA_ROOT / "data" / "raw" / "2330" / "2330_all_l1.npy",
    "2317": _DATA_ROOT / "data" / "raw" / "2317" / "2317_all_l1.npy",
    "MXFC6": _DATA_ROOT / "data" / "raw" / "mxfc6" / "MXFC6_all_l1.npy",
}


# ---------------------------------------------------------------------------
# Helper functions (inline, no batch_alpha_eval dependency)
# ---------------------------------------------------------------------------
def enrich_data(raw: np.ndarray) -> dict[str, np.ndarray]:
    """Extract fields from structured array and add aliases."""
    fields: dict[str, np.ndarray] = {}
    for name in raw.dtype.names or ():
        fields[name] = np.asarray(raw[name], dtype=np.float64)
    # Aliases: depth_ratio alpha expects bid_depth/ask_depth
    fields["bid_depth"] = fields["bid_qty"]
    fields["ask_depth"] = fields["ask_qty"]
    return fields


def compute_forward_returns(
    mid_price: np.ndarray, horizons: tuple[int, ...],
) -> dict[int, np.ndarray]:
    """Compute forward returns for each horizon."""
    n = len(mid_price)
    result: dict[int, np.ndarray] = {}
    for h in horizons:
        fwd = np.full(n, np.nan, dtype=np.float64)
        if h < n:
            fwd[:n - h] = mid_price[h:] - mid_price[:n - h]
        result[h] = fwd
    return result


def information_coefficient(signal: np.ndarray, fwd_ret: np.ndarray) -> float:
    """Spearman rank IC between signal and forward returns."""
    mask = np.isfinite(signal) & np.isfinite(fwd_ret)
    if mask.sum() < 100:
        return 0.0
    corr, _ = sp_stats.spearmanr(signal[mask], fwd_ret[mask])
    return float(corr) if np.isfinite(corr) else 0.0


def signal_sharpe(signal: np.ndarray, fwd_ret: np.ndarray) -> float:
    """Sharpe ratio: mean(signal * fwd_ret) / std(signal * fwd_ret)."""
    mask = np.isfinite(signal) & np.isfinite(fwd_ret)
    if mask.sum() < 100:
        return 0.0
    pnl = signal[mask] * fwd_ret[mask]
    std = pnl.std()
    if std < 1e-15:
        return 0.0
    return float(pnl.mean() / std)


def signal_stats(signal: np.ndarray) -> dict[str, float]:
    """Compute basic signal statistics."""
    finite = signal[np.isfinite(signal)]
    if len(finite) == 0:
        return {"mean": 0.0, "std": 0.0, "nonzero_pct": 0.0, "autocorr_1": 0.0}
    nonzero_pct = float((np.abs(finite) > 1e-10).sum() / len(finite))
    # Lag-1 autocorrelation
    if len(finite) > 2:
        ac = np.corrcoef(finite[:-1], finite[1:])[0, 1]
        ac = float(ac) if np.isfinite(ac) else 0.0
    else:
        ac = 0.0
    return {
        "mean": float(finite.mean()),
        "std": float(finite.std()),
        "nonzero_pct": nonzero_pct,
        "autocorr_1": ac,
    }


def oos_split_eval(
    signal: np.ndarray, fwd_ret: np.ndarray, oos_ratio: float,
) -> dict[str, float]:
    """Compute IS and OOS IC and Sharpe at a given horizon."""
    n = len(signal)
    split = int(n * (1.0 - oos_ratio))
    is_ic = information_coefficient(signal[:split], fwd_ret[:split])
    oos_ic = information_coefficient(signal[split:], fwd_ret[split:])
    is_sharpe = signal_sharpe(signal[:split], fwd_ret[:split])
    oos_sharpe = signal_sharpe(signal[split:], fwd_ret[split:])
    return {
        "is_ic": round(is_ic, 6),
        "oos_ic": round(oos_ic, 6),
        "is_sharpe": round(is_sharpe, 6),
        "oos_sharpe": round(oos_sharpe, 6),
    }


# ---------------------------------------------------------------------------
# Alpha runner
# ---------------------------------------------------------------------------
def run_alpha(alpha: Any, fields: dict[str, np.ndarray], n: int) -> np.ndarray:
    """Run alpha tick-by-tick, return signal array."""
    data_field_names = alpha.manifest.data_fields
    out = np.zeros(n, dtype=np.float64)
    alpha.reset()
    for i in range(n):
        args = [fields[f][i] for f in data_field_names]
        out[i] = alpha.update(*args)
    return out


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------
def classify_tier(oos_ic: float, oos_sharpe: float) -> str:
    """Classify alpha into tier based on OOS metrics at h=5."""
    if oos_ic > 0.02 and oos_sharpe > 2.0:
        return "star"
    if oos_ic > 0.005 or oos_sharpe > 0.5:
        return "promising"
    if oos_ic > 0.002 or oos_sharpe > 0.2:
        return "marginal"
    return "failed"


def is_cross_val_worthy(tier: str) -> bool:
    """Check if alpha is worth cross-validating (promising or star)."""
    return tier in ("promising", "star")


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------
def evaluate_primary(
    alpha_id: str, alpha_cls: type, fields: dict[str, np.ndarray],
    fwd_rets: dict[int, np.ndarray], n: int,
) -> dict[str, Any]:
    """Run full evaluation for one alpha on primary data."""
    print(f"  Running {alpha_id} ({n:,} ticks)...", end=" ", flush=True)
    t0 = time.perf_counter()

    alpha = alpha_cls()
    sig = run_alpha(alpha, fields, n)

    elapsed = time.perf_counter() - t0
    print(f"{elapsed:.1f}s", flush=True)

    # Signal stats
    ss = signal_stats(sig)

    # Per-horizon metrics
    horizons_result: dict[str, dict[str, float]] = {}
    for h in HORIZONS:
        ic = information_coefficient(sig, fwd_rets[h])
        sharpe = signal_sharpe(sig, fwd_rets[h])
        horizons_result[f"h{h}"] = {"ic": round(ic, 6), "sharpe": round(sharpe, 6)}

    # OOS split at h=5
    oos = oos_split_eval(sig, fwd_rets[5], OOS_RATIO)

    tier = classify_tier(oos["oos_ic"], oos["oos_sharpe"])

    return {
        "alpha_id": alpha_id,
        "screening_status": "ok",
        "error": None,
        "primary_data": "TXFC6_all_l1.npy",
        "primary_ticks": n,
        "signal_stats": {k: round(v, 6) for k, v in ss.items()},
        "horizons": horizons_result,
        "oos_split_h5": oos,
        "screening_timestamp": datetime.now(timezone.utc).isoformat(),
        "tier": tier,
        "eval_time_s": round(elapsed, 2),
    }


_cv_cache: dict[str, tuple[dict[str, np.ndarray], dict[int, np.ndarray], int]] = {}


def _load_cross_val_data(
    dataset_name: str, data_path: Path,
) -> tuple[dict[str, np.ndarray], dict[int, np.ndarray], int]:
    """Load and cache cross-validation dataset."""
    if dataset_name not in _cv_cache:
        raw = np.load(str(data_path), allow_pickle=True)
        n = len(raw)
        fields = enrich_data(raw)
        fwd_rets = compute_forward_returns(fields["mid_price"], (5,))
        _cv_cache[dataset_name] = (fields, fwd_rets, n)
    return _cv_cache[dataset_name]


def evaluate_cross_val(
    alpha_id: str, alpha_cls: type, dataset_name: str, data_path: Path,
) -> dict[str, Any]:
    """Run cross-validation on a secondary dataset."""
    print(f"    Cross-val {alpha_id} on {dataset_name}...", end=" ", flush=True)
    t0 = time.perf_counter()

    fields, fwd_rets, n = _load_cross_val_data(dataset_name, data_path)

    alpha = alpha_cls()
    sig = run_alpha(alpha, fields, n)

    ic_h5 = information_coefficient(sig, fwd_rets[5])
    sharpe_h5 = signal_sharpe(sig, fwd_rets[5])
    oos = oos_split_eval(sig, fwd_rets[5], OOS_RATIO)

    elapsed = time.perf_counter() - t0
    print(f"{elapsed:.1f}s (n={n:,}, IC={ic_h5:.4f}, Sharpe={sharpe_h5:.4f})")

    return {
        "dataset": dataset_name,
        "ticks": n,
        "h5_ic": round(ic_h5, 6),
        "h5_sharpe": round(sharpe_h5, 6),
        "oos_ic": oos["oos_ic"],
        "oos_sharpe": oos["oos_sharpe"],
    }


def main() -> None:
    print("=" * 70)
    print("Batch 3 — Depth Geometry Alpha Evaluation")
    print("=" * 70)

    # Load primary data
    print(f"\nLoading primary data: {PRIMARY_DATA}")
    raw = np.load(str(PRIMARY_DATA), allow_pickle=True)
    n = len(raw)
    print(f"  Ticks: {n:,}")

    fields = enrich_data(raw)
    mid_price = fields["mid_price"]

    print("Computing forward returns...")
    fwd_rets = compute_forward_returns(mid_price, HORIZONS)

    # Evaluate each alpha
    results: dict[str, dict[str, Any]] = {}
    for alpha_id, alpha_cls in ALPHAS.items():
        try:
            result = evaluate_primary(alpha_id, alpha_cls, fields, fwd_rets, n)
            results[alpha_id] = result
        except Exception as e:
            print(f"  ERROR: {alpha_id}: {e}")
            results[alpha_id] = {
                "alpha_id": alpha_id,
                "screening_status": "error",
                "error": str(e),
                "primary_data": "TXFC6_all_l1.npy",
                "primary_ticks": n,
                "signal_stats": {},
                "horizons": {},
                "oos_split_h5": {},
                "screening_timestamp": datetime.now(timezone.utc).isoformat(),
                "tier": "failed",
            }

    # Cross-validation for promising alphas
    print("\n" + "-" * 70)
    print("Cross-validation (IC_OOS>0.005 or Sharpe_OOS>0.5)")
    print("-" * 70)

    for alpha_id, result in results.items():
        if result["screening_status"] != "ok":
            continue
        oos = result.get("oos_split_h5", {})
        oos_ic = oos.get("oos_ic", 0.0)
        oos_sharpe = oos.get("oos_sharpe", 0.0)
        if is_cross_val_worthy(result["tier"]):
            print(f"  {alpha_id} qualifies (IC_OOS={oos_ic:.4f}, Sharpe_OOS={oos_sharpe:.4f})")
            cross_val_results = []
            for ds_name, ds_path in CROSS_VAL_DATA.items():
                if ds_path.exists():
                    cv_result = evaluate_cross_val(alpha_id, ALPHAS[alpha_id], ds_name, ds_path)
                    cross_val_results.append(cv_result)
            result["cross_validation"] = cross_val_results
        else:
            print(f"  {alpha_id} skipped (IC_OOS={oos_ic:.4f}, Sharpe_OOS={oos_sharpe:.4f})")

    # Write scorecards
    print("\n" + "-" * 70)
    print("Writing scorecards")
    print("-" * 70)

    for alpha_id, result in results.items():
        scorecard_path = RESEARCH / "alphas" / alpha_id / "scorecard.json"
        scorecard_path.parent.mkdir(parents=True, exist_ok=True)
        with open(scorecard_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"  {scorecard_path}")

    # Summary table
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'Alpha':<25} {'Tier':<12} {'IC_h5':<10} {'Sharpe_h5':<12} {'IC_OOS':<10} {'Sharpe_OOS':<12}")
    print("-" * 81)
    for alpha_id, result in results.items():
        if result["screening_status"] != "ok":
            print(f"{alpha_id:<25} {'ERROR':<12}")
            continue
        h5 = result["horizons"].get("h5", {})
        oos = result.get("oos_split_h5", {})
        print(
            f"{alpha_id:<25} {result['tier']:<12} "
            f"{h5.get('ic', 0):<10.4f} {h5.get('sharpe', 0):<12.4f} "
            f"{oos.get('oos_ic', 0):<10.4f} {oos.get('oos_sharpe', 0):<12.4f}"
        )

    # Cross-val summary for qualifying alphas
    for alpha_id, result in results.items():
        cv = result.get("cross_validation")
        if cv:
            print(f"\n  Cross-validation for {alpha_id}:")
            for entry in cv:
                print(
                    f"    {entry['dataset']:<8} IC={entry['h5_ic']:.4f}  "
                    f"Sharpe={entry['h5_sharpe']:.4f}  "
                    f"OOS_IC={entry['oos_ic']:.4f}  "
                    f"OOS_Sharpe={entry['oos_sharpe']:.4f}"
                )


if __name__ == "__main__":
    sys.exit(main() or 0)
