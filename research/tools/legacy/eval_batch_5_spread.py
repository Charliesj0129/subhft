"""Batch evaluation of 5 spread alphas against real TWSE tick data.

Alphas evaluated:
  1. spread_mean_revert  — spread deviation fade (EMA-8 vs EMA-64)
  2. spread_recovery     — spread recovery speed after widening
  3. spread_excess_toxicity — adverse selection via spread deviation × OFI
  4. spread_adverse_ratio — adverse selection fraction (volatility decomposition)
  5. spread_pressure     — spread widening vs EMA-8 × depth imbalance direction

Usage:
    cd /home/charlie/hft_platform/.claude/worktrees/agent-a32c8497
    uv run python -m research.tools.eval_batch_5_spread
"""
from __future__ import annotations

import json
import math
import sys
import traceback
from pathlib import Path

import numpy as np

# --- project root for imports ---
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
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
# Extra spread enrichment (EMA-derived fields not in base enrich_data)
# ---------------------------------------------------------------------------

_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)


def _ema_vec(arr: np.ndarray, alpha: float) -> np.ndarray:
    """Scalar EMA — same semantics as feature engine rolling EMA."""
    out = np.zeros_like(arr, dtype=np.float64)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = out[i - 1] + alpha * (arr[i] - out[i - 1])
    return out


def enrich_spread_fields(fields: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Add spread-specific derived fields on top of base enrichment."""
    fields["ofi_l1_ema8"] = _ema_vec(fields["ofi_l1_raw"], _EMA_ALPHA_8)
    fields["ofi_l1_cum"] = np.cumsum(fields["ofi_l1_raw"])
    fields["spread_ema8_scaled"] = _ema_vec(fields["spread_scaled"], _EMA_ALPHA_8)
    fields["depth_imbalance_ppm"] = fields["l1_imbalance_ppm"]
    fields["depth_imbalance_ema8_ppm"] = _ema_vec(
        fields["l1_imbalance_ppm"], _EMA_ALPHA_8
    )
    fields["bid_depth"] = fields["bid_qty"]
    fields["ask_depth"] = fields["ask_qty"]
    return fields


# ---------------------------------------------------------------------------
# Generic alpha runner (uses manifest.data_fields)
# ---------------------------------------------------------------------------

def run_alpha(alpha: object, fields: dict[str, np.ndarray], n: int) -> np.ndarray:
    """Run any AlphaProtocol-conforming alpha tick by tick."""
    data_field_names = alpha.manifest.data_fields  # type: ignore[attr-defined]
    out = np.zeros(n, dtype=np.float64)
    alpha.reset()  # type: ignore[attr-defined]
    # Pre-extract arrays for tight loop
    arrs = [fields[f] for f in data_field_names]
    report_interval = max(1, n // 10)
    for i in range(n):
        vals = [a[i] for a in arrs]
        out[i] = alpha.update(*vals)  # type: ignore[attr-defined]
        if i > 0 and i % report_interval == 0:
            print(f"      {i:,}/{n:,} ({100*i//n}%)", flush=True)
    return out


# ---------------------------------------------------------------------------
# Alpha factory
# ---------------------------------------------------------------------------

def _load_alphas() -> dict[str, object]:
    """Import and instantiate all 5 spread alphas."""
    from research.alphas.spread_mean_revert.impl import SpreadMeanRevertAlpha
    from research.alphas.spread_recovery.impl import SpreadRecoveryAlpha
    from research.alphas.spread_excess_toxicity.impl import SpreadExcessToxicityAlpha
    from research.alphas.spread_adverse_ratio.impl import SpreadAdverseRatioAlpha
    from research.alphas.spread_pressure.impl import SpreadPressureAlpha

    return {
        "spread_mean_revert": SpreadMeanRevertAlpha(),
        "spread_recovery": SpreadRecoveryAlpha(),
        "spread_excess_toxicity": SpreadExcessToxicityAlpha(),
        "spread_adverse_ratio": SpreadAdverseRatioAlpha(),
        "spread_pressure": SpreadPressureAlpha(),
    }


# ---------------------------------------------------------------------------
# Tiering logic
# ---------------------------------------------------------------------------

def tier_alpha(ic_oos: float, sharpe_oos: float) -> str:
    """Classify alpha based on OOS IC and Sharpe."""
    if ic_oos > 0.02 and sharpe_oos > 2.0:
        return "Star"
    if ic_oos > 0.005 or sharpe_oos > 0.5:
        return "Promising"
    if ic_oos > 0.002 or sharpe_oos > 0.2:
        return "Marginal"
    return "Failed"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

# Resolve data paths — fall back to main repo if worktree lacks data.
_REPO_DATA = ROOT / "research" / "data" / "raw"
_MAIN_DATA = Path("/home/charlie/hft_platform/research/data/raw")
_DATA_BASE = _REPO_DATA if (_REPO_DATA / "txfc6" / "TXFC6_all_l1.npy").exists() else _MAIN_DATA
PRIMARY_DATA = _DATA_BASE / "txfc6" / "TXFC6_all_l1.npy"
CROSS_VAL_DATA = {
    "2330": _DATA_BASE / "2330" / "2330_all_l1.npy",
    "2317": _DATA_BASE / "2317" / "2317_all_l1.npy",
    "MXFC6": _DATA_BASE / "mxfc6" / "MXFC6_all_l1.npy",
}


MAX_TICKS = 2_000_000  # Cap to avoid OOM on large datasets


def load_and_enrich(path: Path, max_ticks: int = MAX_TICKS) -> tuple[dict[str, np.ndarray], int]:
    """Load .npy data and enrich with all derived fields."""
    print(f"  Loading {path.name}...")
    raw = np.load(str(path))
    total = len(raw)
    if total > max_ticks:
        print(f"  Subsampling {total:,} -> {max_ticks:,} ticks (last {max_ticks:,})")
        raw = raw[-max_ticks:]
    n = len(raw)
    print(f"  {n:,} ticks, fields: {list(raw.dtype.names)}")
    fields = enrich_data(raw)
    fields = enrich_spread_fields(fields)
    return fields, n


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate_on_dataset(
    dataset_name: str,
    fields: dict[str, np.ndarray],
    n: int,
    alphas: dict[str, object],
    horizons: tuple[int, ...] = (1, 5, 10, 20, 50),
    oos_ratio: float = 0.3,
) -> dict[str, dict]:
    """Evaluate all alphas on one dataset."""
    print(f"\n{'='*70}")
    print(f"  Dataset: {dataset_name} ({n:,} ticks)")
    print(f"{'='*70}")

    fwd_rets = compute_forward_returns(fields["mid_price"], horizons)
    results: dict[str, dict] = {}

    for alpha_id, alpha in alphas.items():
        print(f"\n  --- {alpha_id} ---")
        try:
            signal = run_alpha(alpha, fields, n)
            stats = signal_stats(signal)
            print(
                f"    stats: mean={stats['mean']:.6f}, std={stats['std']:.6f}, "
                f"nonzero={stats['nonzero_pct']:.1f}%, AC1={stats['autocorr_1']:.3f}"
            )

            horizon_results = {}
            for h in horizons:
                ic = information_coefficient(signal, fwd_rets[h])
                sharpe = signal_sharpe(signal, fwd_rets[h])
                horizon_results[f"h{h}"] = {"ic": round(ic, 4), "sharpe": round(sharpe, 2)}
                print(f"    h={h:3d}: IC={ic:+.4f}  Sharpe={sharpe:+.1f}")

            oos = oos_split_eval(signal, fwd_rets[5], oos_ratio)
            print(
                f"    OOS (h=5): IC_IS={oos['ic_is']:+.4f} IC_OOS={oos['ic_oos']:+.4f} "
                f"Sharpe_IS={oos['sharpe_is']:+.1f} Sharpe_OOS={oos['sharpe_oos']:+.1f}"
            )

            tier = tier_alpha(oos["ic_oos"], oos["sharpe_oos"])
            print(f"    => Tier: {tier}")

            results[alpha_id] = {
                "signal_stats": stats,
                "horizons": horizon_results,
                "oos_split_h5": oos,
                "tier": tier,
                "status": "ok",
            }
        except Exception as e:
            print(f"    ERROR: {e}")
            traceback.print_exc()
            results[alpha_id] = {"status": "error", "error": str(e)}

    return results


def write_scorecards(
    primary_results: dict[str, dict],
    cross_val_results: dict[str, dict[str, dict]],
    output_dir: Path,
) -> None:
    """Write scorecard.json for each alpha."""
    for alpha_id, res in primary_results.items():
        if res["status"] != "ok":
            continue

        scorecard_dir = output_dir / alpha_id
        scorecard_dir.mkdir(parents=True, exist_ok=True)

        # Collect cross-validation results
        cv_summary = {}
        for cv_name, cv_results in cross_val_results.items():
            cv_res = cv_results.get(alpha_id, {})
            if cv_res.get("status") == "ok":
                cv_summary[cv_name] = {
                    "ic_oos_h5": cv_res["oos_split_h5"]["ic_oos"],
                    "sharpe_oos_h5": cv_res["oos_split_h5"]["sharpe_oos"],
                    "tier": cv_res["tier"],
                }

        scorecard = {
            "alpha_id": alpha_id,
            "primary_dataset": "TXFC6_all_l1",
            "primary_ticks": MAX_TICKS,  # Evaluated on capped subset
            "horizons": res["horizons"],
            "oos_split_h5": res["oos_split_h5"],
            "signal_stats": res["signal_stats"],
            "tier": res["tier"],
            "cross_validation": cv_summary,
            "evaluation_date": "2026-03-15",
            "latency_profile": "shioaji_sim_p95_v2026-03-04",
        }

        scorecard_path = scorecard_dir / "scorecard.json"
        with open(scorecard_path, "w") as f:
            json.dump(scorecard, f, indent=2, default=str)
        print(f"  Wrote {scorecard_path}")


def main() -> None:
    print("=" * 70)
    print("  Batch 5 Spread Alpha Evaluation")
    print("=" * 70)

    # --- Load alphas ---
    alphas = _load_alphas()
    horizons = (1, 5, 10, 20, 50)

    # --- Primary evaluation on TXFC6 ---
    fields, n = load_and_enrich(PRIMARY_DATA)
    primary_results = evaluate_on_dataset("TXFC6", fields, n, alphas, horizons)

    # --- Find promising alphas for cross-validation ---
    promising_ids = [
        aid for aid, r in primary_results.items()
        if r.get("status") == "ok" and r.get("tier") in ("Star", "Promising", "Marginal")
    ]
    print(f"\nPromising alphas for cross-validation: {promising_ids}")

    # --- Cross-validation ---
    cross_val_results: dict[str, dict[str, dict]] = {}
    if promising_ids:
        for cv_name, cv_path in CROSS_VAL_DATA.items():
            if cv_path.exists():
                cv_fields, cv_n = load_and_enrich(cv_path)
                # Fresh alpha instances for each CV dataset (clean state)
                cv_alphas = {aid: _load_alphas()[aid] for aid in promising_ids}
                cross_val_results[cv_name] = evaluate_on_dataset(
                    cv_name, cv_fields, cv_n, cv_alphas, horizons
                )
            else:
                print(f"  SKIP cross-validation on {cv_name}: {cv_path} not found")

    # --- Summary table ---
    print(f"\n{'='*90}")
    print(f"{'SPREAD ALPHA BATCH COMPARISON':^90}")
    print(f"{'='*90}")
    header = f"{'Alpha':<28} {'Tier':<12} {'IC_OOS(h5)':>10} {'Sh_OOS(h5)':>10} {'IC(h1)':>8} {'IC(h10)':>8} {'IC(h50)':>8} {'AC1':>6}"
    print(header)
    print("-" * 90)

    ranked = []
    for alpha_id, r in primary_results.items():
        if r["status"] != "ok":
            print(f"{alpha_id:<28} {'ERROR':<12}")
            continue
        ic_oos = r["oos_split_h5"]["ic_oos"]
        sh_oos = r["oos_split_h5"]["sharpe_oos"]
        ic_h1 = r["horizons"]["h1"]["ic"]
        ic_h10 = r["horizons"]["h10"]["ic"]
        ic_h50 = r["horizons"]["h50"]["ic"]
        ac1 = r["signal_stats"]["autocorr_1"]
        tier = r["tier"]
        print(
            f"{alpha_id:<28} {tier:<12} {ic_oos:>+10.4f} {sh_oos:>+10.1f} "
            f"{ic_h1:>+8.4f} {ic_h10:>+8.4f} {ic_h50:>+8.4f} {ac1:>6.3f}"
        )
        ranked.append((alpha_id, ic_oos, sh_oos, tier))

    ranked.sort(key=lambda x: abs(x[1]), reverse=True)
    print(f"\n--- Ranked by |IC_OOS| (h=5 ticks) ---")
    for i, (alpha_id, ic, sh, tier) in enumerate(ranked, 1):
        marker = " [STAR]" if tier == "Star" else (" [PROMISING]" if tier == "Promising" else "")
        print(f"  {i}. {alpha_id}: IC_OOS={ic:+.4f}, Sharpe_OOS={sh:+.1f}, tier={tier}{marker}")

    # --- Cross-validation summary ---
    if cross_val_results:
        print(f"\n--- Cross-validation Summary ---")
        for cv_name, cv_res in cross_val_results.items():
            print(f"  {cv_name}:")
            for alpha_id, r in cv_res.items():
                if r.get("status") == "ok":
                    ic = r["oos_split_h5"]["ic_oos"]
                    sh = r["oos_split_h5"]["sharpe_oos"]
                    print(f"    {alpha_id}: IC_OOS={ic:+.4f}, Sharpe_OOS={sh:+.1f}, tier={r['tier']}")

    # --- Write scorecards ---
    scorecard_dir = ROOT / "research" / "alphas"
    print(f"\nWriting scorecards...")
    write_scorecards(primary_results, cross_val_results, scorecard_dir)

    # --- Save full results JSON ---
    out_path = ROOT / "research" / "results" / "eval_batch_5_spread.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    full_output = {
        "primary_dataset": str(PRIMARY_DATA),
        "primary_ticks": n,
        "horizons": list(horizons),
        "oos_ratio": 0.3,
        "primary_results": primary_results,
        "cross_validation": cross_val_results,
    }
    with open(out_path, "w") as f:
        json.dump(full_output, f, indent=2, default=str)
    print(f"Saved full results: {out_path}")


if __name__ == "__main__":
    main()
