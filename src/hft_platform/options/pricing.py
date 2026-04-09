"""Black-76 option pricing and implied volatility solver.

Float exception: Per Architecture Governance Rule 25 §11, float is permitted
in this module for offline research computation.

References:
    Black (1976) — The pricing of commodity contracts.
    Brenner & Subrahmanyam (1988) — A simple formula to compute the implied
        standard deviation.
"""

from __future__ import annotations

import math

from scipy.optimize import brentq
from scipy.stats import norm


def black76_price(F: float, K: float, T: float, sigma: float, r: float, cp: str) -> float:
    """Price a European option on a futures contract using the Black-76 model.

    Args:
        F:     Futures price.
        K:     Strike price.
        T:     Time to expiry in years (≥ 0).
        sigma: Annualised volatility (> 0 for non-trivial pricing).
        r:     Risk-free rate (continuously compounded).
        cp:    Option type — ``"C"`` for call, ``"P"`` for put.

    Returns:
        Option price (float, non-negative).

    Raises:
        ValueError: If *cp* is neither ``"C"`` nor ``"P"``.
    """
    cp = cp.upper()
    if cp not in ("C", "P"):
        raise ValueError(f"cp must be 'C' or 'P', got {cp!r}")

    disc = math.exp(-r * T)

    # Handle boundary cases before computing d1/d2 to avoid division by zero.
    if T <= 0 or sigma <= 0:
        if cp == "C":
            return max(F - K, 0.0)
        return max(K - F, 0.0)

    sqrt_T = math.sqrt(T)
    vol_sqrt_T = sigma * sqrt_T
    d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / vol_sqrt_T
    d2 = d1 - vol_sqrt_T

    if cp == "C":
        return disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    # PUT
    return disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def _vega_b76(F: float, K: float, T: float, sigma: float, r: float) -> float:
    """Black-76 vega (∂price/∂sigma).

    Returns 0.0 when sigma ≤ 0 or T ≤ 0 (finite difference falls back to
    Brent in that case).
    """
    if sigma <= 0 or T <= 0:
        return 0.0

    disc = math.exp(-r * T)
    sqrt_T = math.sqrt(T)
    vol_sqrt_T = sigma * sqrt_T
    d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / vol_sqrt_T
    # vega = disc * F * n(d1) * sqrt(T)
    return disc * F * norm.pdf(d1) * sqrt_T


def solve_iv(
    market_price: float,
    F: float,
    K: float,
    T: float,
    r: float,
    cp: str,
    tick_size: float = 1.0,
) -> float:
    """Solve for implied volatility given a market option price.

    Uses Newton-Raphson with a Brenner-Subrahmanyam initial guess, falling
    back to Brent's method if Newton does not converge.

    Args:
        market_price: Observed market price of the option.
        F:            Futures price.
        K:            Strike price.
        T:            Time to expiry in years.
        r:            Risk-free rate.
        cp:           ``"C"`` or ``"P"``.
        tick_size:    Minimum price increment for the instrument.  Prices
                      below ``0.5 * tick_size`` are treated as deep-OTM and
                      return NaN.

    Returns:
        Implied volatility (annualised) or ``math.nan`` when the solve fails
        or inputs are degenerate.
    """
    NAN = math.nan

    # --- Input guards ---
    if market_price <= 0:
        return NAN

    if T <= 0:
        return NAN

    if market_price < 0.5 * tick_size:
        return NAN

    # Intrinsic guard (discounted)
    disc = math.exp(-r * T)
    if cp.upper() == "C":
        intrinsic = disc * max(F - K, 0.0)
    else:
        intrinsic = disc * max(K - F, 0.0)

    if market_price < intrinsic - 1e-10:
        return NAN

    # --- Brenner-Subrahmanyam initial guess ---
    # σ₀ ≈ √(2π/T) * C/F  (for calls at ATM; safe for puts via abs)
    sigma = math.sqrt(2.0 * math.pi / T) * abs(market_price) / F
    # Clamp to a reasonable range
    sigma = max(1e-4, min(sigma, 5.0))

    _SIGMA_LOW = 1e-6
    _SIGMA_HIGH = 10.0
    _MAX_ITER = 50
    _TOL = 1e-8

    # --- Newton-Raphson ---
    for _ in range(_MAX_ITER):
        price = black76_price(F, K, T, sigma, r, cp)
        diff = price - market_price
        if abs(diff) < _TOL:
            return sigma

        vega = _vega_b76(F, K, T, sigma, r)
        if vega < 1e-12:
            # Vega too small — abandon Newton, go to Brent
            break

        sigma_new = sigma - diff / vega
        sigma = max(_SIGMA_LOW, min(sigma_new, _SIGMA_HIGH))

    # --- Brent fallback ---
    def objective(s: float) -> float:
        return black76_price(F, K, T, s, r, cp) - market_price

    try:
        sigma = brentq(objective, _SIGMA_LOW, _SIGMA_HIGH, xtol=_TOL, maxiter=200)
        return sigma
    except ValueError:
        return NAN
