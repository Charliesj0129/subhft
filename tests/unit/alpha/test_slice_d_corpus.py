"""Slice-D Task 7b: synthetic signal corpus integrity tests.

Verifies the deterministic fixture corpus produced by
``scripts/generate_slice_d_signal_corpus.py`` and committed under
``research/experiments/_slice_d_fixtures/`` so DoD-D3 (cluster pair detection
on the R47 family) is reproducible in CI.

Contracts under test:
  * directory layout matches ``ExperimentTracker(base_dir=...).runs_dir``
  * tracker discovers all 15 alphas via ``latest_signals_by_alpha()``
  * R47-family pairwise Pearson correlations land in [0.75, 0.85]
  * non-R47 vs R47 mean ``|corr|`` is < 0.10 (independence sanity)
  * total ``signals.npy`` bytes < 2 MB (plan T7b budget)
  * loaded signal dtype is ``float32`` on disk (tracker upcasts to float64)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha.experiments import ExperimentTracker

CORPUS_BASE = Path("research/experiments/_slice_d_fixtures")
EXPECTED_ALPHAS = {
    "c14_txf_frontmonth_native_maker",
    "c17_tmf_frontmonth_native_maker",
    "c1_revalidation_txfd6_chavez_casillas_adaptive",
    "c27_vol_amplification_on_c14",
    "c30_txf_maker_tmf_hedge_pair",
    "c32b_tob_survival_refresh_regime_gate",
    "c33_txfd6_solo_passive_maker",
    "c60_tmfd6_r47_minimal_inst_rt",
    "c63_txfd6_r47_tight_spread",
    "c68_txf_rollover_back_front_maker",
    "c72_tmfd6_queue_position_aware",
    "c74_txf_tmf_basis_mean_reversion",
    "fill_prob_filter",
    "r47_maker_pivot",
    "r52_amhp_dynamic_spread",
}
R47_FAMILY = (
    "r47_maker_pivot",
    "c60_tmfd6_r47_minimal_inst_rt",
    "c63_txfd6_r47_tight_spread",
    "c72_tmfd6_queue_position_aware",
)
NON_R47 = tuple(sorted(EXPECTED_ALPHAS - set(R47_FAMILY)))
CORPUS_BYTE_BUDGET = 2 * 1024 * 1024  # 2 MB plan budget.


@pytest.fixture(scope="module")
def tracker() -> ExperimentTracker:
    return ExperimentTracker(base_dir=str(CORPUS_BASE))


@pytest.fixture(scope="module")
def signals(tracker: ExperimentTracker) -> dict[str, np.ndarray]:
    return tracker.latest_signals_by_alpha()


def test_corpus_directory_exists() -> None:
    assert (CORPUS_BASE / "runs").is_dir(), (
        "corpus runs/ dir missing -- regenerate with `python scripts/generate_slice_d_signal_corpus.py`"
    )


def test_corpus_has_15_alphas(signals: dict[str, np.ndarray]) -> None:
    assert set(signals.keys()) == EXPECTED_ALPHAS, (
        f"expected 15 alphas {sorted(EXPECTED_ALPHAS)}, got {sorted(signals.keys())}"
    )


def test_corpus_r47_family_correlated(signals: dict[str, np.ndarray]) -> None:
    family_signals = [signals[a] for a in R47_FAMILY]
    pairs = []
    for i, ai in enumerate(family_signals):
        for j, aj in enumerate(family_signals):
            if j <= i:
                continue
            rho = float(np.corrcoef(ai, aj)[0, 1])
            pairs.append((R47_FAMILY[i], R47_FAMILY[j], rho))
    assert pairs, "R47 family should have >= 1 pair"
    for ai_id, aj_id, rho in pairs:
        assert 0.75 <= rho <= 0.85, f"R47 pair {ai_id} vs {aj_id} corr={rho:.4f} outside [0.75, 0.85]"


def test_corpus_non_r47_uncorrelated_with_r47(signals: dict[str, np.ndarray]) -> None:
    cross_abs: list[float] = []
    for nr in NON_R47:
        for rf in R47_FAMILY:
            cross_abs.append(abs(float(np.corrcoef(signals[nr], signals[rf])[0, 1])))
    mean_abs = float(np.mean(cross_abs))
    assert mean_abs < 0.10, f"non-R47 vs R47 mean |corr| = {mean_abs:.4f} too high (expected < 0.10 for independence)"


def test_corpus_byte_size_under_budget() -> None:
    runs_dir = CORPUS_BASE / "runs"
    total = sum(p.stat().st_size for p in runs_dir.rglob("signals.npy"))
    assert total > 0, "no signals.npy files found under corpus runs/"
    assert total < CORPUS_BYTE_BUDGET, (
        f"corpus signals.npy total {total} bytes exceeds 2 MB budget {CORPUS_BYTE_BUDGET}"
    )


def test_corpus_signal_dtype_is_float32_on_disk() -> None:
    # latest_signals_by_alpha() upcasts to float64; verify the on-disk dtype
    # directly to confirm the storage budget is honoured (float64 would double
    # the size).
    runs_dir = CORPUS_BASE / "runs"
    npy_files = list(runs_dir.rglob("signals.npy"))
    assert len(npy_files) == 15, f"expected 15 signals.npy files, got {len(npy_files)}"
    sample = np.load(npy_files[0], allow_pickle=False)
    assert sample.dtype == np.float32, f"signal {npy_files[0]} dtype = {sample.dtype}, expected float32"
    assert sample.shape == (10_000,), f"signal shape {sample.shape}, expected (10000,)"
