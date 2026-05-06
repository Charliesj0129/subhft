"""Generate the Slice-D synthetic signal corpus (T7b / DoD-D3).

Produces deterministic float32 signals for the 15 manifest-bearing alphas in
``research/alphas/``. The R47 family (``r47_maker_pivot``,
``c60_tmfd6_r47_minimal_inst_rt``, ``c63_txfd6_r47_tight_spread``,
``c72_tmfd6_queue_position_aware``) shares a latent factor wired to give
pairwise Pearson correlation of approximately 0.81 by construction; the other
11 alphas are independent Gaussians.

All randomness uses ``numpy.random.default_rng(42)``. Timestamps in
``meta.json`` are hardcoded to ``2026-05-05T00:00:00+00:00`` so the artifacts
are byte-deterministic across machines and re-runs (idempotent).

Layout note (deviation from plan §7 T7b path)
---------------------------------------------
Plan listed
``research/experiments/_slice_d_fixtures/<alpha_id>/runs/<run_id>/...`` but
``ExperimentTracker`` (see ``src/hft_platform/alpha/experiments.py``) scans
``base_dir/runs/*/meta.json`` flat and groups by the ``alpha_id`` field
inside each ``meta.json`` -- not from path components. This script writes the
real-tracker layout
``research/experiments/_slice_d_fixtures/runs/2026-05-05_seed42_<alpha_id>/``
so ``ExperimentTracker(base_dir='research/experiments/_slice_d_fixtures').
latest_signals_by_alpha()`` discovers all 15 alphas without modification.

Outputs (per alpha)
-------------------
``research/experiments/_slice_d_fixtures/runs/2026-05-05_seed42_<alpha_id>/``
  ``meta.json``               -- ExperimentRun payload (deterministic)
  ``signals.npy``             -- shape (10000,) float32
  ``scorecard.json``          -- ``{}`` (stub)
  ``backtest_report.json``    -- ``{}`` (stub)

Usage
-----
``python scripts/generate_slice_d_signal_corpus.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants -- all deterministic; no env / clock reads.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_BASE_REL = Path("research/experiments/_slice_d_fixtures")
CORPUS_BASE_ABS = REPO_ROOT / CORPUS_BASE_REL

SEED = 42
SIGNAL_LENGTH = 10_000
RUN_ID_PREFIX = "2026-05-05_seed42_"
DETERMINISTIC_TIMESTAMP = "2026-05-05T00:00:00+00:00"
CONFIG_HASH = "synthetic_seed42"

# Latent-factor mix to pin pairwise corr at 0.81 (a^2 / (a^2 + b^2) = 0.81).
# a=0.9, b=sqrt(1 - 0.81)=sqrt(0.19) gives unit variance and corr=0.81 exactly.
LATENT_WEIGHT = 0.9
IID_WEIGHT = float(np.sqrt(1.0 - LATENT_WEIGHT**2))

R47_FAMILY: tuple[str, ...] = (
    "r47_maker_pivot",
    "c60_tmfd6_r47_minimal_inst_rt",
    "c63_txfd6_r47_tight_spread",
    "c72_tmfd6_queue_position_aware",
)

ALL_ALPHAS: tuple[str, ...] = (
    # R47 family (latent factor)
    "c60_tmfd6_r47_minimal_inst_rt",
    "c63_txfd6_r47_tight_spread",
    "c72_tmfd6_queue_position_aware",
    "r47_maker_pivot",
    # Independent (alphabetical, manifest-bearing)
    "c14_txf_frontmonth_native_maker",
    "c17_tmf_frontmonth_native_maker",
    "c1_revalidation_txfd6_chavez_casillas_adaptive",
    "c27_vol_amplification_on_c14",
    "c30_txf_maker_tmf_hedge_pair",
    "c32b_tob_survival_refresh_regime_gate",
    "c33_txfd6_solo_passive_maker",
    "c68_txf_rollover_back_front_maker",
    "c74_txf_tmf_basis_mean_reversion",
    "fill_prob_filter",
    "r52_amhp_dynamic_spread",
)

CORPUS_BYTE_BUDGET = 2 * 1024 * 1024  # 2 MB total budget per plan T7b.

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _build_signal(
    *,
    alpha_id: str,
    latent: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Generate the (deterministic) signal array for ``alpha_id``.

    R47 family members get ``LATENT_WEIGHT * latent + IID_WEIGHT * iid`` where
    ``iid`` is a fresh standard-normal draw; non-R47 alphas get pure
    independent standard-normal noise. Both branches return ``float32``.
    """
    if alpha_id in R47_FAMILY:
        iid = rng.standard_normal(SIGNAL_LENGTH)
        sig = LATENT_WEIGHT * latent + IID_WEIGHT * iid
    else:
        sig = rng.standard_normal(SIGNAL_LENGTH)
    return np.asarray(sig, dtype=np.float32)


def _write_run(*, alpha_id: str, signal: np.ndarray) -> dict[str, str]:
    """Write the per-alpha run dir; return relative paths used in meta.json."""
    run_id = f"{RUN_ID_PREFIX}{alpha_id}"
    run_dir_rel = CORPUS_BASE_REL / "runs" / run_id
    run_dir_abs = REPO_ROOT / run_dir_rel
    run_dir_abs.mkdir(parents=True, exist_ok=True)

    signals_rel = run_dir_rel / "signals.npy"
    scorecard_rel = run_dir_rel / "scorecard.json"
    backtest_rel = run_dir_rel / "backtest_report.json"
    meta_rel = run_dir_rel / "meta.json"

    # signals.npy -- deterministic float32 array; allow_pickle=False per project rules.
    np.save(REPO_ROOT / signals_rel, signal, allow_pickle=False)

    # Stub scorecard / backtest report (empty dict, schema-permitted).
    (REPO_ROOT / scorecard_rel).write_text(json.dumps({}, indent=2, sort_keys=True))
    (REPO_ROOT / backtest_rel).write_text(json.dumps({}, indent=2, sort_keys=True))

    meta_payload: dict[str, object] = {
        "run_id": run_id,
        "alpha_id": alpha_id,
        "config_hash": CONFIG_HASH,
        "timestamp": DETERMINISTIC_TIMESTAMP,
        "data_paths": ["synthetic"],
        "metrics": {},
        "gate_status": {},
        "scorecard_path": str(scorecard_rel),
        "backtest_report_path": str(backtest_rel),
        "signals_path": str(signals_rel),
        "equity_path": None,
    }
    (REPO_ROOT / meta_rel).write_text(json.dumps(meta_payload, indent=2, sort_keys=True))

    return {
        "alpha_id": alpha_id,
        "run_id": run_id,
        "run_dir": str(run_dir_rel),
        "signals_path": str(signals_rel),
    }


def _empirical_corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation between two 1-D arrays."""
    return float(np.corrcoef(a, b)[0, 1])


def _total_signal_bytes() -> int:
    """Sum of bytes for all ``signals.npy`` under the corpus dir."""
    runs_dir = CORPUS_BASE_ABS / "runs"
    if not runs_dir.exists():
        return 0
    return sum(p.stat().st_size for p in runs_dir.rglob("signals.npy"))


def _verify_with_tracker() -> dict[str, np.ndarray]:
    """Round-trip the corpus through ``ExperimentTracker`` (smoke test).

    Imported lazily so the script can run in a stripped-down environment
    (the test file does the strict ``pytest`` assertions).
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from hft_platform.alpha.experiments import ExperimentTracker

    tracker = ExperimentTracker(base_dir=str(CORPUS_BASE_ABS))
    return tracker.latest_signals_by_alpha()


# ---------------------------------------------------------------------------
# Entrypoint.
# ---------------------------------------------------------------------------


def main() -> int:
    if len(set(ALL_ALPHAS)) != 15:
        print(f"FATAL: expected 15 unique alpha_ids, got {len(set(ALL_ALPHAS))}", file=sys.stderr)
        return 2

    rng = np.random.default_rng(SEED)
    # Latent factor must be drawn FIRST so the consumption order across runs
    # is identical regardless of which alphas we iterate over later.
    latent = rng.standard_normal(SIGNAL_LENGTH).astype(np.float32)

    # Iterate in a stable order so per-alpha rng draws are deterministic.
    written: list[dict[str, str]] = []
    for alpha_id in sorted(ALL_ALPHAS):
        signal = _build_signal(alpha_id=alpha_id, latent=latent, rng=rng)
        written.append(_write_run(alpha_id=alpha_id, signal=signal))

    # ----------------------------------------------------------------------
    # Self-check: byte budget, dtype, ExperimentTracker round-trip, R47 corr.
    # ----------------------------------------------------------------------
    total_bytes = _total_signal_bytes()
    print(f"signals.npy total bytes: {total_bytes} (budget {CORPUS_BYTE_BUDGET})")
    if total_bytes > CORPUS_BYTE_BUDGET:
        print("FATAL: corpus exceeds 2 MB budget", file=sys.stderr)
        return 3

    sigs = _verify_with_tracker()
    if len(sigs) != 15:
        print(f"FATAL: ExperimentTracker found {len(sigs)} alphas, expected 15", file=sys.stderr)
        return 4

    missing = [a for a in ALL_ALPHAS if a not in sigs]
    if missing:
        print(f"FATAL: tracker missing alphas: {missing}", file=sys.stderr)
        return 5

    # Empirical corr table for the R47 quartet.
    print("\nR47-family pairwise Pearson correlations:")
    family = list(R47_FAMILY)
    rho_anchor = _empirical_corr(sigs["r47_maker_pivot"], sigs["c60_tmfd6_r47_minimal_inst_rt"])
    for i, ai in enumerate(family):
        for aj in family[i + 1 :]:
            print(f"  {ai}  <->  {aj} : rho = {_empirical_corr(sigs[ai], sigs[aj]):+.4f}")

    # Anchor correlation must land in [0.78, 0.84] so DoD-D3 reliably finds
    # the cluster at threshold 0.7.
    if not (0.78 <= rho_anchor <= 0.84):
        print(
            f"FATAL: anchor corr r47_maker_pivot vs c60 = {rho_anchor:.4f} outside [0.78, 0.84]",
            file=sys.stderr,
        )
        return 6

    # Sanity: any non-R47 vs R47 should be near-zero (loose bound; report only).
    non_r47 = [a for a in ALL_ALPHAS if a not in R47_FAMILY]
    cross_abs: list[float] = []
    for nr in non_r47:
        for rf in R47_FAMILY:
            cross_abs.append(abs(_empirical_corr(sigs[nr], sigs[rf])))
    avg_cross = float(np.mean(cross_abs)) if cross_abs else 0.0
    print(f"\nMean |corr| (non-R47 x R47): {avg_cross:.4f}")

    print(f"\nWrote {len(written)} runs under {CORPUS_BASE_REL}/runs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
