"""Batch 2 evaluation: Flow & Impact alphas against TXFC6 data.

Alphas: hawkes_ofi_impact, kyle_lambda, flow_persistence, transient_impact_game
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

# ---------------------------------------------------------------------------
# Project root setup
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Data paths
# ---------------------------------------------------------------------------
# Data may live in the main repo (not the worktree), so fall back.
_DATA_DIR = _ROOT / "research" / "data" / "raw"
_MAIN_DATA_DIR = Path("/home/charlie/hft_platform/research/data/raw")
if not (_DATA_DIR / "txfc6" / "TXFC6_all_l1.npy").exists() and _MAIN_DATA_DIR.exists():
    _DATA_DIR = _MAIN_DATA_DIR
_PRIMARY_DATA = _DATA_DIR / "txfc6" / "TXFC6_all_l1.npy"
_CROSS_VAL_DATA = {
    "2330": _DATA_DIR / "2330" / "2330_all_l1.npy",
    "2317": _DATA_DIR / "2317" / "2317_all_l1.npy",
    "MXFC6": _DATA_DIR / "mxfc6" / "MXFC6_all_l1.npy",
}

_HORIZONS = (1, 5, 10, 20, 50)
_OOS_RATIO = 0.3
# Cap ticks to avoid excessive runtime in pure-Python tick loop.
_MAX_TICKS = 500_000

# ---------------------------------------------------------------------------
# EMA helper
# ---------------------------------------------------------------------------
_EMA_ALPHA_8 = 1.0 - math.exp(-1.0 / 8.0)


def _ema_vec(arr: np.ndarray, alpha: float) -> np.ndarray:
    out = np.zeros_like(arr, dtype=np.float64)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = out[i - 1] + alpha * (arr[i] - out[i - 1])
    return out


# ---------------------------------------------------------------------------
# Data enrichment
# ---------------------------------------------------------------------------

def enrich_data(data: np.ndarray) -> dict[str, np.ndarray]:
    """Enrich structured array into dict of named fields."""
    fields: dict[str, np.ndarray] = {}
    for name in data.dtype.names:
        fields[name] = np.asarray(data[name], dtype=np.float64)

    mid = fields["mid_price"]
    bid_px = fields["bid_px"]
    ask_px = fields["ask_px"]

    fields["microprice_x2"] = (
        bid_px * fields["ask_qty"] + ask_px * fields["bid_qty"]
    ) / np.maximum(fields["bid_qty"] + fields["ask_qty"], 1.0)
    fields["microprice_x2"] = (fields["microprice_x2"] * 2).astype(np.int64).astype(np.float64)

    spread = ask_px - bid_px
    fields["spread_scaled"] = (spread * 10000).astype(np.int64).astype(np.float64)
    fields["mid_price_x2"] = (mid * 2).astype(np.int64).astype(np.float64)

    # OFI L1
    dbid = np.concatenate([[0.0], np.diff(fields["bid_qty"])])
    dask = np.concatenate([[0.0], np.diff(fields["ask_qty"])])
    fields["ofi_l1_raw"] = dbid - dask

    # L1 imbalance
    total = fields["bid_qty"] + fields["ask_qty"]
    fields["l1_imbalance_ppm"] = np.where(
        total > 0,
        ((fields["bid_qty"] - fields["ask_qty"]) / total * 1_000_000).astype(np.int64).astype(np.float64),
        0.0,
    )

    # Extra enrichments
    fields["ofi_l1_cum"] = np.cumsum(fields["ofi_l1_raw"])
    fields["ofi_l1_ema8"] = _ema_vec(fields["ofi_l1_raw"], _EMA_ALPHA_8)
    fields["current_return"] = np.concatenate(
        [[0.0], np.diff(np.log(np.maximum(mid, 1e-12)))]
    )
    fields["bid_depth"] = fields["bid_qty"]
    fields["ask_depth"] = fields["ask_qty"]
    fields["price"] = mid
    fields["spread_ema8_scaled"] = _ema_vec(fields["spread_scaled"], _EMA_ALPHA_8)
    fields["depth_imbalance_ppm"] = fields["l1_imbalance_ppm"]
    fields["depth_imbalance_ema8_ppm"] = _ema_vec(fields["l1_imbalance_ppm"], _EMA_ALPHA_8)

    return fields


# ---------------------------------------------------------------------------
# Forward returns
# ---------------------------------------------------------------------------

def compute_forward_returns(
    mid_price: np.ndarray, horizons: tuple[int, ...] = _HORIZONS
) -> dict[int, np.ndarray]:
    log_mid = np.log(np.maximum(mid_price, 1e-12))
    result: dict[int, np.ndarray] = {}
    n = len(mid_price)
    for h in horizons:
        fwd = np.full(n, np.nan, dtype=np.float64)
        if h < n:
            fwd[: n - h] = log_mid[h:] - log_mid[: n - h]
        result[h] = fwd
    return result


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def information_coefficient(signal: np.ndarray, fwd_ret: np.ndarray) -> float:
    mask = np.isfinite(signal) & np.isfinite(fwd_ret) & (signal != 0.0)
    if mask.sum() < 100:
        return 0.0
    corr, _ = stats.spearmanr(signal[mask], fwd_ret[mask])
    return float(corr) if np.isfinite(corr) else 0.0


def signal_sharpe(signal: np.ndarray, fwd_ret: np.ndarray) -> float:
    mask = np.isfinite(signal) & np.isfinite(fwd_ret) & (signal != 0.0)
    if mask.sum() < 100:
        return 0.0
    pnl = np.sign(signal[mask]) * fwd_ret[mask]
    mu = np.mean(pnl)
    sd = np.std(pnl)
    if sd < 1e-15:
        return 0.0
    return float(mu / sd * np.sqrt(252 * 4000))  # annualized assuming ~4000 ticks/day


def signal_stats(signal: np.ndarray) -> dict[str, float]:
    finite = signal[np.isfinite(signal)]
    if len(finite) == 0:
        return {"mean": 0.0, "std": 0.0, "nonzero_pct": 0.0, "autocorr_1": 0.0}
    nz = np.count_nonzero(finite)
    ac = float(np.corrcoef(finite[:-1], finite[1:])[0, 1]) if len(finite) > 2 else 0.0
    return {
        "mean": float(np.mean(finite)),
        "std": float(np.std(finite)),
        "nonzero_pct": float(nz / len(finite) * 100),
        "autocorr_1": float(ac) if np.isfinite(ac) else 0.0,
    }


def oos_split_eval(
    signal: np.ndarray, fwd_ret: np.ndarray, oos_ratio: float = _OOS_RATIO
) -> dict[str, float]:
    n = len(signal)
    split = int(n * (1.0 - oos_ratio))
    return {
        "ic_is": information_coefficient(signal[:split], fwd_ret[:split]),
        "ic_oos": information_coefficient(signal[split:], fwd_ret[split:]),
        "sharpe_is": signal_sharpe(signal[:split], fwd_ret[:split]),
        "sharpe_oos": signal_sharpe(signal[split:], fwd_ret[split:]),
    }


# ---------------------------------------------------------------------------
# Alpha runner
# ---------------------------------------------------------------------------

def run_alpha(alpha: Any, fields: dict[str, np.ndarray], n: int) -> np.ndarray:
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

def classify_tier(ic_oos: float, sharpe_oos: float) -> str:
    if ic_oos > 0.02 and sharpe_oos > 2.0:
        return "star"
    if ic_oos > 0.005 or sharpe_oos > 0.5:
        return "promising"
    if ic_oos > 0.002 or sharpe_oos > 0.2:
        return "marginal"
    return "failed"


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def load_alphas() -> list[Any]:
    from research.alphas.flow_persistence.impl import ALPHA_CLASS as FlowPersistence
    from research.alphas.hawkes_ofi_impact.impl import ALPHA_CLASS as HawkesOfi
    from research.alphas.kyle_lambda.impl import ALPHA_CLASS as KyleLambda
    from research.alphas.transient_impact_game.impl import ALPHA_CLASS as TransientImpact

    return [HawkesOfi(), KyleLambda(), FlowPersistence(), TransientImpact()]


def evaluate_alpha_on_data(
    alpha: Any, fields: dict[str, np.ndarray], n: int, label: str
) -> dict[str, Any]:
    aid = alpha.manifest.alpha_id
    print(f"  [{label}] Running {aid} on {n:,} ticks ...", end=" ", flush=True)
    t0 = time.monotonic()
    signal = run_alpha(alpha, fields, n)
    elapsed = time.monotonic() - t0
    print(f"{elapsed:.1f}s")

    fwd_rets = compute_forward_returns(fields["mid_price"], _HORIZONS)

    horizons_result: dict[str, dict[str, float]] = {}
    for h in _HORIZONS:
        horizons_result[f"h{h}"] = {
            "ic": information_coefficient(signal, fwd_rets[h]),
            "sharpe": signal_sharpe(signal, fwd_rets[h]),
        }

    oos = oos_split_eval(signal, fwd_rets[5], _OOS_RATIO)
    stats_result = signal_stats(signal)

    return {
        "signal_stats": stats_result,
        "horizons": horizons_result,
        "oos_split_h5": oos,
    }


def main() -> None:
    print("=" * 72)
    print("Batch 2: Flow & Impact Alpha Evaluation")
    print("=" * 72)

    # Load primary data
    print(f"\nLoading primary data: {_PRIMARY_DATA}")
    data = np.load(str(_PRIMARY_DATA), allow_pickle=True)
    raw_n = len(data)
    if raw_n > _MAX_TICKS:
        step = raw_n // _MAX_TICKS
        data = data[::step][:_MAX_TICKS]
        print(f"  Raw ticks: {raw_n:,} -> subsampled to {len(data):,} (step={step})")
    n = len(data)
    print(f"  Ticks: {n:,}")

    fields = enrich_data(data)
    print("  Enrichment complete.")

    alphas = load_alphas()
    results: dict[str, dict[str, Any]] = {}

    # Primary evaluation
    for alpha in alphas:
        aid = alpha.manifest.alpha_id
        res = evaluate_alpha_on_data(alpha, fields, n, "TXFC6")
        res["primary_data"] = "TXFC6_all_l1.npy"
        res["primary_ticks"] = n
        results[aid] = res

    # Print summary
    print("\n" + "=" * 72)
    print("PRIMARY RESULTS (TXFC6)")
    print("=" * 72)
    print(f"{'Alpha':<25} {'IC_h5':>8} {'IC_OOS':>8} {'Sharpe_OOS':>11} {'Tier':>10}")
    print("-" * 72)

    promising_ids: list[str] = []
    for aid, res in results.items():
        ic_h5 = res["horizons"]["h5"]["ic"]
        ic_oos = res["oos_split_h5"]["ic_oos"]
        sharpe_oos = res["oos_split_h5"]["sharpe_oos"]
        tier = classify_tier(ic_oos, sharpe_oos)
        res["tier"] = tier
        print(f"{aid:<25} {ic_h5:>8.4f} {ic_oos:>8.4f} {sharpe_oos:>11.2f} {tier:>10}")
        if tier in ("star", "promising"):
            promising_ids.append(aid)

    # Cross-validation for promising alphas
    if promising_ids:
        print(f"\n{'=' * 72}")
        print(f"CROSS-VALIDATION for: {', '.join(promising_ids)}")
        print("=" * 72)

        # Build lookup for fresh alpha instances per cross-val run
        alpha_by_id = {a.manifest.alpha_id: type(a) for a in alphas}

        for sym, path in _CROSS_VAL_DATA.items():
            if not path.exists():
                print(f"  [{sym}] Data not found at {path}, skipping.")
                continue
            print(f"\n  Loading {sym} data: {path}")
            cv_data = np.load(str(path), allow_pickle=True)
            raw_cv_n = len(cv_data)
            if raw_cv_n > _MAX_TICKS:
                step = raw_cv_n // _MAX_TICKS
                cv_data = cv_data[::step][:_MAX_TICKS]
                print(f"  Raw ticks: {raw_cv_n:,} -> subsampled to {len(cv_data):,}")
            cv_n = len(cv_data)
            print(f"  Ticks: {cv_n:,}")
            cv_fields = enrich_data(cv_data)

            for aid in promising_ids:
                alpha = alpha_by_id[aid]()
                cv_res = evaluate_alpha_on_data(alpha, cv_fields, cv_n, sym)
                if "cross_val" not in results[aid]:
                    results[aid]["cross_val"] = {}
                results[aid]["cross_val"][sym] = {
                    "ticks": cv_n,
                    "ic_h5": cv_res["horizons"]["h5"]["ic"],
                    "sharpe_h5": cv_res["horizons"]["h5"]["sharpe"],
                    "ic_oos": cv_res["oos_split_h5"]["ic_oos"],
                    "sharpe_oos": cv_res["oos_split_h5"]["sharpe_oos"],
                }

        # Print cross-val summary
        print(f"\n{'=' * 72}")
        print("CROSS-VALIDATION SUMMARY")
        print("=" * 72)
        print(f"{'Alpha':<25} {'Symbol':>8} {'IC_OOS':>8} {'Sharpe_OOS':>11}")
        print("-" * 72)
        for aid in promising_ids:
            cv = results[aid].get("cross_val", {})
            for sym, cv_res in cv.items():
                print(
                    f"{aid:<25} {sym:>8} {cv_res['ic_oos']:>8.4f} "
                    f"{cv_res['sharpe_oos']:>11.2f}"
                )

    # Write scorecards
    print(f"\n{'=' * 72}")
    print("WRITING SCORECARDS")
    print("=" * 72)
    ts = datetime.now(timezone.utc).isoformat()

    for aid, res in results.items():
        scorecard = {
            "alpha_id": aid,
            "screening_status": "ok",
            "error": None,
            "primary_data": res["primary_data"],
            "primary_ticks": res["primary_ticks"],
            "signal_stats": res["signal_stats"],
            "horizons": res["horizons"],
            "oos_split_h5": res["oos_split_h5"],
            "screening_timestamp": ts,
            "tier": res["tier"],
        }
        if "cross_val" in res:
            scorecard["cross_validation"] = res["cross_val"]

        out_path = _ROOT / "research" / "alphas" / aid / "scorecard.json"
        out_path.write_text(json.dumps(scorecard, indent=2) + "\n")
        print(f"  {out_path.relative_to(_ROOT)}")

    print("\nDone.")


if __name__ == "__main__":
    main()
