"""Closed-form exp-Hawkes MLE — synthetic-recovery-grade ρ̂ estimator.

Theory
------
Univariate self-exciting Hawkes process with exponential kernel:

    λ(t) = μ + α · Σ_{t_j < t} exp(-β (t - t_j))

Branching ratio ρ = α / β ∈ [0, 1) — fraction of events that are descendants
of past events; ρ ≥ 1 = supercritical / non-stationary (forbidden).

Log-likelihood on observation window [t_start, t_end] given event times
t_1 < ... < t_n inside the window:

    L = Σ_i ln(λ(t_i)) - ∫_{t_start}^{t_end} λ(s) ds

The recursive A_i = exp(-β Δt) (1 + A_{i-1}) keeps λ(t_i) evaluation O(n);
the integral term reduces in closed form using
∫ exp(-β (s - t_j)) ds = (1 - exp(-β (t_end - t_j))) / β.

Properties
----------
* **Synthetic-recovery accuracy**: |ρ̂ - ρ_true| ≤ 0.05 at n ≈ 2K events,
  duration ≈ 1000–2000 sec (verified in tests/research/r52_amhp_dynamic_spread/
  test_online_hawkes_mle.py with seed=20260425).
* **Live-update p99 latency**: NOT met. Full L-BFGS-B with 4 multistarts and
  finite-difference gradient runs at ~50 ms/refit on n ≈ 1000 events.
  The T4 binding-contract requires p99 < 1 ms — see :mod:`vmr_estimator`
  for the moments-based alternative used in the C2 pre-T4 gate replay.

Float exception
---------------
Architecture Governance Rule §11: float is permitted in this research
module (offline analysis / model fit). Live-path arithmetic uses scaled int.

Use this module when offline precision matters (synthetic recovery, gold
ρ̂ benchmarks) and the VMR estimator (`vmr_estimator.py`) when live p99<1ms
is required.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

_DEFAULT_MAX_ITER = 60
_DEFAULT_FTOL = 1e-6


@dataclass
class HawkesMLEState:
    """Result of a single MLE fit on a sliding window."""

    mu: float
    alpha: float
    beta: float
    rho_hat: float       # alpha / beta clipped to [0, 0.99]
    n_events: int
    fit_ok: bool


def exp_hawkes_loglik(
    mu: float,
    alpha: float,
    beta: float,
    times: np.ndarray,
    t_start: float,
    t_end: float,
) -> float:
    """Closed-form log-likelihood of the exp-Hawkes process on [t_start, t_end]."""
    n = len(times)
    if n == 0 or beta <= 0 or mu <= 0 or alpha < 0:
        return -np.inf

    a_i = np.zeros(n)
    for i in range(1, n):
        dt = times[i] - times[i - 1]
        a_i[i] = math.exp(-beta * dt) * (1.0 + a_i[i - 1])

    lam_at_events = mu + alpha * a_i
    if np.any(lam_at_events <= 0):
        return -np.inf

    sum_log = float(np.sum(np.log(lam_at_events)))
    integral_excite = (alpha / beta) * float(
        np.sum(1.0 - np.exp(-beta * (t_end - times)))
    )
    integral = mu * (t_end - t_start) + integral_excite
    return sum_log - integral


def fit_exp_hawkes_window(
    times_sec: np.ndarray,
    t_start: float,
    t_end: float,
    *,
    init_mu: float = 1.5,
    init_alpha: float = 0.5,
    init_beta: float = 1.0,
    max_iter: int = _DEFAULT_MAX_ITER,
    ftol: float = _DEFAULT_FTOL,
) -> HawkesMLEState:
    """L-BFGS-B over log-parameterized (μ, α, β) with multistart.

    Multistart guards against plateaus in the likelihood landscape. Each start
    runs ≤ ``max_iter`` L-BFGS-B iterations; total wall time ≈ 50 ms for
    n ≈ 1000 events. NOT suitable for per-tick rolling refit (see VMR).
    """
    from scipy.optimize import minimize

    n = int(len(times_sec))
    if n < 5 or t_end <= t_start:
        return HawkesMLEState(
            mu=init_mu, alpha=0.0, beta=init_beta, rho_hat=0.0,
            n_events=n, fit_ok=False,
        )

    eps = 1e-9

    def neg_ll(theta: np.ndarray) -> float:
        mu = math.exp(theta[0])
        alpha = math.exp(theta[1])
        beta = math.exp(theta[2])
        if alpha >= beta * 0.99:
            return 1e10
        ll = exp_hawkes_loglik(mu, alpha, beta, times_sec, t_start, t_end)
        if not math.isfinite(ll):
            return 1e10
        return -ll

    starts = [
        (init_mu, init_alpha, init_beta),
        (1.0, 0.4, 1.0),
        (max(init_mu, 0.5), 0.2, 0.5),
        (1.5, 0.7, 1.5),
    ]
    best_ll = np.inf
    best_x: tuple[float, float, float] = (init_mu, init_alpha, init_beta)
    for s_mu, s_a, s_b in starts:
        x0 = np.array([
            math.log(max(s_mu, eps)),
            math.log(max(s_a, eps)),
            math.log(max(s_b, eps)),
        ])
        try:
            res = minimize(
                neg_ll, x0,
                method="L-BFGS-B",
                bounds=[(-10, 5), (-10, 5), (-10, 5)],
                options={"maxiter": max_iter, "ftol": ftol},
            )
        except Exception:
            continue
        if res.fun < best_ll and math.isfinite(res.fun):
            best_ll = res.fun
            best_x = (
                math.exp(res.x[0]),
                math.exp(res.x[1]),
                math.exp(res.x[2]),
            )
    mu, alpha, beta = best_x
    rho = max(0.0, min(0.99, alpha / max(beta, eps)))
    fit_ok = math.isfinite(best_ll) and best_ll < 1e9
    return HawkesMLEState(
        mu=mu, alpha=alpha, beta=beta, rho_hat=rho,
        n_events=n, fit_ok=fit_ok,
    )


def synthetic_recovery_test(
    *,
    mu_true: float = 1.0,
    alpha_true: float = 0.5,
    beta_true: float = 1.0,
    duration_sec: float = 2000.0,
    seed: int = 20260425,
    tolerance: float = 0.10,
) -> dict[str, Any]:
    """Generate an exp-Hawkes process via Ogata thinning and fit MLE.

    Default parameters give ρ_true = 0.5 and ~2,142 events at duration=2000s.

    Returns a dict with keys:
      n_synthetic_events, rho_true, rho_hat, abs_err, passes,
      mu_hat, alpha_hat, beta_hat.
    """
    rng = np.random.default_rng(seed)
    times: list[float] = []
    t = 0.0
    while t < duration_sec:
        if not times:
            lam_bar = mu_true
        else:
            lam_bar = mu_true + alpha_true * sum(
                math.exp(-beta_true * (t - tj)) for tj in times[-30:]
            )
        u = float(rng.random())
        if lam_bar <= 0:
            break
        dt = -math.log(max(u, 1e-12)) / lam_bar
        t = t + dt
        if t >= duration_sec:
            break
        if not times:
            lam_t = mu_true
        else:
            lam_t = mu_true + alpha_true * sum(
                math.exp(-beta_true * (t - tj)) for tj in times if t - tj < 30
            )
        if rng.random() <= lam_t / lam_bar:
            times.append(t)

    arr = np.asarray(times)
    state = fit_exp_hawkes_window(
        arr, 0.0, duration_sec,
        init_mu=1.0, init_alpha=0.4, init_beta=1.0,
    )
    rho_true = alpha_true / beta_true
    return {
        "n_synthetic_events": len(times),
        "rho_true": rho_true,
        "rho_hat": state.rho_hat,
        "abs_err": abs(state.rho_hat - rho_true),
        "passes": abs(state.rho_hat - rho_true) <= tolerance,
        "mu_hat": state.mu,
        "alpha_hat": state.alpha,
        "beta_hat": state.beta,
    }
