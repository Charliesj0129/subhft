"""Synthetic recovery + sanity tests for OnlineHawkesMLE / VMR estimators.

T4 binding-contract item from docs/alpha-research/round-2-hawkes-amhp/artifacts/
t2_devils_advocate_c2.md §"T4 binding contract":

  "OnlineHawkesMLE class with closed-form exp-Hawkes MLE on rolling 5-min window.
   Synthetic-recovery tests: ρ̂ recovery ρ_true ± 0.05 at n=10K events.
   Live ρ̂(t) telemetry stream sampled at ≥ 1 Hz."

Synthetic-recovery requirement is met by `online_hawkes_mle.synthetic_recovery_test`
at the (relaxed for finite-sample noise) tolerance of 0.10 — tightening to 0.05
requires longer simulated duration (20K+ events) which would slow the test
to ~10s. The 0.10 band corresponds to typical L-BFGS-B finite-window noise on
2K-event simulations; ρ̂ accuracy improves with √n so 10K+ events comfortably
clears 0.05.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from research.alphas.r52_amhp_dynamic_spread.online_hawkes_mle import (
    fit_exp_hawkes_window,
    synthetic_recovery_test,
)
from research.alphas.r52_amhp_dynamic_spread.vmr_estimator import (
    OnlineHawkesVMR,
    vmr_rho_hat,
)

# ----------------------------------------------------------------------------
# OnlineHawkesMLE — closed-form exp-Hawkes MLE recovery
# ----------------------------------------------------------------------------


def test_synthetic_recovery_default_seed_passes_at_010_tolerance() -> None:
    """ρ̂ recovers ρ_true=0.5 within ±0.10 on a 2000-sec / ~2K-event simulation.

    Tightening to 0.05 requires longer duration; tested separately.
    """
    res = synthetic_recovery_test(
        mu_true=1.0, alpha_true=0.5, beta_true=1.0,
        duration_sec=2000.0, seed=20260425, tolerance=0.10,
    )
    assert res["passes"] is True
    assert res["rho_true"] == pytest.approx(0.5)
    assert res["abs_err"] <= 0.10
    # Sanity: estimator should not collapse to ρ̂ = 0.
    assert res["rho_hat"] > 0.20


def test_synthetic_recovery_tightens_to_005_at_high_n() -> None:
    """At duration=10000s (~10K+ events) the MLE recovers within ±0.05.

    Slow test (~5-10s wall) — kept as a separate test so unit-test sweep
    is fast.
    """
    res = synthetic_recovery_test(
        mu_true=1.0, alpha_true=0.5, beta_true=1.0,
        duration_sec=10000.0, seed=20260425, tolerance=0.05,
    )
    assert res["abs_err"] <= 0.05
    assert res["n_synthetic_events"] >= 10000


def test_synthetic_recovery_multi_seed_robust_at_010_tolerance() -> None:
    """Across several seeds, ±0.10 is the worst-case finite-sample band."""
    for seed in [1, 2, 3, 7, 11]:
        res = synthetic_recovery_test(
            duration_sec=2000.0, seed=seed, tolerance=0.10,
        )
        assert res["abs_err"] <= 0.10, (
            f"seed={seed} ρ̂={res['rho_hat']:.3f} err={res['abs_err']:.3f}"
        )


def test_mle_returns_fit_ok_false_on_short_window() -> None:
    """Window with < 5 events: estimator must mark fit_ok=False, not crash."""
    arr = np.array([1.0, 2.0, 3.0])
    state = fit_exp_hawkes_window(arr, 0.0, 100.0)
    assert state.fit_ok is False
    assert state.n_events == 3


def test_mle_branching_ratio_clipped_to_subcritical() -> None:
    """ρ̂ must be ∈ [0, 0.99] regardless of MLE result."""
    res = synthetic_recovery_test(
        mu_true=0.1, alpha_true=0.95, beta_true=1.0,
        duration_sec=2000.0, seed=42,
    )
    assert 0.0 <= res["rho_hat"] <= 0.99


# ----------------------------------------------------------------------------
# VMR estimator — fast live ρ̂ approximation
# ----------------------------------------------------------------------------


def test_vmr_rho_hat_returns_zero_on_poisson() -> None:
    """Poisson process → VMR ≈ 1 → ρ̂ ≈ 0."""
    rng = np.random.default_rng(seed=0)
    rate = 5.0
    duration_sec = 300
    n_events = rng.poisson(rate * duration_sec)
    times_sec = np.sort(rng.uniform(0, duration_sec, size=n_events))
    times_ns = (times_sec * 1e9).astype(np.int64).tolist()
    end_ns = duration_sec * 1_000_000_000
    rho = vmr_rho_hat(times_ns, end_ns, window_sec=duration_sec, subwindow_sec=10)
    assert rho < 0.20, f"Poisson VMR yielded ρ̂={rho:.3f}, expected ~ 0"


def test_vmr_rho_hat_detects_clustering() -> None:
    """Bursty / clustered arrivals → VMR > 1 → ρ̂ > 0."""
    duration_sec = 300
    bursts_per_sec = 2.0
    burst_size = 8
    rng = np.random.default_rng(seed=0)
    burst_centers = np.sort(rng.uniform(0, duration_sec, int(bursts_per_sec * duration_sec)))
    times_sec_list: list[float] = []
    for c in burst_centers:
        offsets = rng.exponential(0.05, burst_size)
        times_sec_list.extend(c + offsets)
    times_sec = np.array(sorted(t for t in times_sec_list if 0 <= t < duration_sec))
    times_ns = (times_sec * 1e9).astype(np.int64).tolist()
    end_ns = duration_sec * 1_000_000_000
    rho = vmr_rho_hat(times_ns, end_ns, window_sec=duration_sec, subwindow_sec=10)
    assert rho > 0.30, f"Clustered tape yielded ρ̂={rho:.3f}, expected > 0.3"


def test_vmr_estimator_class_p99_under_1ms() -> None:
    """OnlineHawkesVMR.maybe_refit p99 latency must clear T4 budget < 1 ms.

    Standalone bench: 1000 forced refits over a populated 5-min window.
    """
    est = OnlineHawkesVMR(window_sec=300, subwindow_sec=10, refit_every_ns=0)
    rng = np.random.default_rng(seed=0)
    base_ns = 1_700_000_000_000_000_000
    times_ns = base_ns + np.cumsum(rng.exponential(1e7, 5000)).astype(np.int64)

    for t in times_ns:
        est.update(int(t))

    # Force refit on 1000 evenly-spaced timestamps.
    refit_targets = times_ns[-1000:]
    latencies_ns: list[int] = []
    for t in refit_targets:
        t0 = time.perf_counter_ns()
        est._last_fit_ns = -1  # force refit
        est.maybe_refit(int(t))
        latencies_ns.append(time.perf_counter_ns() - t0)
    latencies_ms = np.array(latencies_ns) / 1e6
    p99 = float(np.percentile(latencies_ms, 99))
    assert p99 < 1.0, f"VMR refit p99={p99:.3f}ms exceeds 1ms budget"


def test_online_hawkes_vmr_basic_api() -> None:
    """Smoke test for the OnlineHawkesVMR class API."""
    est = OnlineHawkesVMR(window_sec=60, subwindow_sec=2, refit_every_ns=int(1e8))
    base = 1_700_000_000_000_000_000
    for i in range(200):
        est.update(base + i * 100_000_000)  # 100ms apart -> 10/sec
    rho = est.maybe_refit(base + 200 * 100_000_000)
    assert 0.0 <= rho <= 0.99
    assert est.n_events_in_window > 0
    assert est.get_rho_hat() == rho
