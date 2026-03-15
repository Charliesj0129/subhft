"""Batch 7 evaluation: Toxicity alphas (5 alphas) on TXFC6 L1 data.

Alphas: toxic_flow, flow_toxicity_ratio, toxicity_multiscale,
        toxicity_timescale_divergence, toxicity_acceleration

Usage:
    uv run python research/tools/eval_batch_7_toxicity.py
"""
from __future__ import annotations

import json
import math
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from research.tools.batch_alpha_eval import (
    compute_forward_returns,
    enrich_data,
    information_coefficient,
    oos_split_eval,
    signal_sharpe,
    signal_stats,
)

# ---------------------------------------------------------------------------
# Extra enrichment for toxicity alphas
# ---------------------------------------------------------------------------
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)


def _ema_vec(arr: np.ndarray, alpha: float) -> np.ndarray:
    out = np.zeros_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = out[i - 1] + alpha * (arr[i] - out[i - 1])
    return out


def enrich_toxicity_fields(fields: dict[str, np.ndarray]) -> None:
    """Add toxicity-specific enrichment fields (in-place).

    Only ofi_l1_ema8 is required by toxic_flow and toxicity_acceleration.
    Other alphas use fields already provided by enrich_data().
    """
    fields["ofi_l1_ema8"] = _ema_vec(fields["ofi_l1_raw"], _EMA_ALPHA_8)


# ---------------------------------------------------------------------------
# Generic alpha runner
# ---------------------------------------------------------------------------
def run_alpha_generic(alpha_obj: object, fields: dict, n: int) -> np.ndarray:
    """Run alpha tick-by-tick using manifest.data_fields dispatch."""
    data_field_names = alpha_obj.manifest.data_fields  # type: ignore[attr-defined]
    out = np.zeros(n, dtype=np.float64)
    alpha_obj.reset()  # type: ignore[attr-defined]

    # Pre-extract field arrays for speed
    field_arrays = []
    for f in data_field_names:
        if f not in fields:
            print(f"  WARNING: missing field '{f}' for {alpha_obj.manifest.alpha_id}")
            return out
        field_arrays.append(fields[f])

    for i in range(n):
        args = [fa[i] for fa in field_arrays]
        out[i] = alpha_obj.update(*args)  # type: ignore[attr-defined]
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
# Cross-validation
# ---------------------------------------------------------------------------
CROSS_VAL_FILES = {
    "2330": "research/data/raw/2330/2330_all_l1.npy",
    "2317": "research/data/raw/2317/2317_all_l1.npy",
    "MXFC6": "research/data/raw/mxfc6/MXFC6_all_l1.npy",
}


def cross_validate(
    alpha_id: str,
    alpha_cls: type,
    base_dir: Path,
) -> dict:
    """Run cross-validation on secondary datasets."""
    results = {}
    for label, rel_path in CROSS_VAL_FILES.items():
        data_path = base_dir / rel_path
        if not data_path.exists():
            results[label] = {"status": "file_not_found"}
            continue
        try:
            data = np.load(str(data_path), allow_pickle=True)
            n = len(data)
            fields = enrich_data(data)
            enrich_toxicity_fields(fields)
            alpha = alpha_cls()
            signal = run_alpha_generic(alpha, fields, n)
            fwd_rets = compute_forward_returns(fields["mid_price"], (5,))
            oos = oos_split_eval(signal, fwd_rets[5], 0.3)
            results[label] = {
                "ticks": n,
                "ic_oos": round(oos["ic_oos"], 6),
                "sharpe_oos": round(oos["sharpe_oos"], 2),
                "status": "ok",
            }
        except Exception as e:
            results[label] = {"status": "error", "error": str(e)}
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    data_path = ROOT / "research" / "data" / "raw" / "txfc6" / "TXFC6_all_l1.npy"
    if not data_path.exists():
        print(f"Data file not found: {data_path}")
        sys.exit(1)

    print(f"Loading {data_path}...")
    data = np.load(str(data_path), allow_pickle=True)
    n = len(data)
    print(f"  {n:,} ticks, fields: {list(data.dtype.names)}")

    print("Enriching data...")
    fields = enrich_data(data)
    enrich_toxicity_fields(fields)

    horizons = (1, 5, 10, 20, 50)
    print(f"Computing forward returns at horizons {horizons}...")
    fwd_rets = compute_forward_returns(fields["mid_price"], horizons)

    # Import alpha classes
    from research.alphas.toxic_flow.impl import ToxicFlowAlpha
    from research.alphas.flow_toxicity_ratio.impl import FlowToxicityRatioAlpha
    from research.alphas.toxicity_multiscale.impl import ToxicityMultiscaleAlpha
    from research.alphas.toxicity_timescale_divergence.impl import (
        ToxicityTimescaleDivergenceAlpha,
    )
    from research.alphas.toxicity_acceleration.impl import ToxicityAccelerationAlpha

    alpha_configs = [
        {"id": "toxic_flow", "cls": ToxicFlowAlpha},
        {"id": "flow_toxicity_ratio", "cls": FlowToxicityRatioAlpha},
        {"id": "toxicity_multiscale", "cls": ToxicityMultiscaleAlpha},
        {"id": "toxicity_timescale_divergence", "cls": ToxicityTimescaleDivergenceAlpha},
        {"id": "toxicity_acceleration", "cls": ToxicityAccelerationAlpha},
    ]

    all_results = {}

    for cfg in alpha_configs:
        alpha_id = cfg["id"]
        alpha_cls = cfg["cls"]
        print(f"\n{'=' * 60}")
        print(f"Running: {alpha_id}")
        t0 = time.monotonic()

        try:
            alpha = alpha_cls()
            signal = run_alpha_generic(alpha, fields, n)
            elapsed = time.monotonic() - t0
            print(f"  Computed in {elapsed:.1f}s")

            stats = signal_stats(signal)
            print(
                f"  Signal stats: mean={stats['mean']:.6f}, std={stats['std']:.6f}, "
                f"nonzero={stats['nonzero_pct']:.1f}%, AC1={stats['autocorr_1']:.3f}"
            )

            # IC and Sharpe at each horizon
            horizon_results = {}
            for h in horizons:
                ic = information_coefficient(signal, fwd_rets[h])
                sh = signal_sharpe(signal, fwd_rets[h])
                horizon_results[f"h{h}"] = {"ic": round(ic, 6), "sharpe": round(sh, 2)}
                print(f"  h={h:3d}: IC={ic:+.4f}  Sharpe={sh:+.1f}")

            # OOS split at h=5
            oos = oos_split_eval(signal, fwd_rets[5], 0.3)
            print(
                f"  OOS split (h=5): IC_IS={oos['ic_is']:+.4f} IC_OOS={oos['ic_oos']:+.4f} "
                f"Sharpe_IS={oos['sharpe_is']:+.1f} Sharpe_OOS={oos['sharpe_oos']:+.1f}"
            )

            tier = classify_tier(oos["ic_oos"], oos["sharpe_oos"])
            print(f"  Tier: {tier}")

            result = {
                "alpha_id": alpha_id,
                "screening_status": "ok",
                "error": None,
                "primary_data": "TXFC6_all_l1.npy",
                "primary_ticks": n,
                "signal_stats": stats,
                "horizons": horizon_results,
                "oos_split_h5": {
                    "ic_is": round(oos["ic_is"], 6),
                    "ic_oos": round(oos["ic_oos"], 6),
                    "sharpe_is": round(oos["sharpe_is"], 2),
                    "sharpe_oos": round(oos["sharpe_oos"], 2),
                },
                "screening_timestamp": datetime.now(timezone.utc).isoformat(),
                "tier": tier,
            }

            # Cross-validate promising alphas
            if oos["ic_oos"] > 0.005 or oos["sharpe_oos"] > 0.5:
                print("  Cross-validating (promising signal)...")
                xval = cross_validate(alpha_id, alpha_cls, ROOT)
                result["cross_validation"] = xval
                for label, xr in xval.items():
                    if xr.get("status") == "ok":
                        print(
                            f"    {label}: IC_OOS={xr['ic_oos']:+.4f} "
                            f"Sharpe_OOS={xr['sharpe_oos']:+.1f}"
                        )
                    else:
                        print(f"    {label}: {xr.get('status', 'unknown')}")

            all_results[alpha_id] = result

        except Exception as e:
            elapsed = time.monotonic() - t0
            print(f"  ERROR ({elapsed:.1f}s): {e}")
            traceback.print_exc()
            all_results[alpha_id] = {
                "alpha_id": alpha_id,
                "screening_status": "error",
                "error": str(e),
                "primary_data": "TXFC6_all_l1.npy",
                "primary_ticks": n,
                "signal_stats": {
                    "mean": 0,
                    "std": 0,
                    "nonzero_pct": 0,
                    "autocorr_1": 0,
                },
                "horizons": {},
                "oos_split_h5": {
                    "ic_is": 0,
                    "ic_oos": 0,
                    "sharpe_is": 0,
                    "sharpe_oos": 0,
                },
                "screening_timestamp": datetime.now(timezone.utc).isoformat(),
                "tier": "failed",
            }

    # Summary
    print(f"\n{'=' * 80}")
    print(f"{'TOXICITY BATCH SUMMARY':^80}")
    print(f"{'=' * 80}")
    print(
        f"{'Alpha':<35} {'Tier':<10} {'IC_OOS(h5)':>10} {'Sh_OOS(h5)':>12} "
        f"{'IC(h1)':>8} {'AC1':>6}"
    )
    print("-" * 80)
    for aid, r in all_results.items():
        if r["screening_status"] == "error":
            print(f"{aid:<35} {'ERROR':<10}")
            continue
        ic_oos = r["oos_split_h5"]["ic_oos"]
        sh_oos = r["oos_split_h5"]["sharpe_oos"]
        ic_h1 = r["horizons"].get("h1", {}).get("ic", 0)
        ac1 = r["signal_stats"]["autocorr_1"]
        print(
            f"{aid:<35} {r['tier']:<10} {ic_oos:>+10.4f} {sh_oos:>+12.1f} "
            f"{ic_h1:>+8.4f} {ac1:>6.3f}"
        )

    # Write scorecards
    for aid, r in all_results.items():
        scorecard_path = ROOT / "research" / "alphas" / aid / "scorecard.json"
        scorecard_path.parent.mkdir(parents=True, exist_ok=True)
        with open(scorecard_path, "w") as f:
            json.dump(r, f, indent=2, default=str)
        print(f"\nScorecard written: {scorecard_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
