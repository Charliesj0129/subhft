"""Black-76 closed-form Greeks and portfolio aggregation.

Float exception: Per Architecture Governance Rule 25 §11, float is permitted
in this module for offline research computation.

References:
    Black (1976) — The pricing of commodity contracts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm

from hft_platform.options.pricing import black76_price


@dataclass(slots=True)
class GreeksResult:
    """Black-76 first-order Greeks for a single option."""

    delta: float
    gamma: float
    theta: float  # per calendar day, sign convention: negative for long
    vega: float   # per 1% move in implied vol (per 0.01 sigma)
    rho: float    # per 1% move in risk-free rate (per 0.01)


@dataclass(slots=True)
class PositionGreeks:
    """Greeks scaled by position quantity."""

    symbol: str
    qty: int
    greeks: GreeksResult


@dataclass(slots=True)
class AggregatedGreeks:
    """Portfolio-level aggregated Greeks, scaled by contract multiplier."""

    net_delta: float
    net_gamma: float
    net_theta_ntd: float   # NTD per calendar day
    net_vega_ntd: float    # NTD per 1% vol move
    positions: tuple[PositionGreeks, ...]


def compute_greeks(
    F: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
    cp: str,
) -> GreeksResult:
    """Compute Black-76 closed-form Greeks for a European futures option.

    Args:
        F:     Futures price.
        K:     Strike price.
        T:     Time to expiry in years (>= 0).
        sigma: Annualised volatility (> 0 for non-trivial Greeks).
        r:     Risk-free rate (continuously compounded).
        cp:    Option type — ``"C"`` for call, ``"P"`` for put.

    Returns:
        GreeksResult with delta, gamma, theta (per day), vega (per 1% vol), rho.
    """
    disc = math.exp(-r * T)

    # Degenerate case: expired or zero vol
    if T <= 0.0 or sigma <= 0.0:
        if cp == "C":
            delta = disc if F > K else 0.0
        else:
            delta = -disc if F < K else 0.0
        return GreeksResult(delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    N = norm.cdf
    n = norm.pdf  # standard normal density

    nd1 = n(d1)
    Nd1 = N(d1)
    Nd2 = N(d2)

    # Delta
    if cp == "C":
        delta = disc * Nd1
    else:
        delta = disc * (Nd1 - 1.0)

    # Gamma — identical for call and put
    gamma = disc * nd1 / (F * sigma * sqrt_T)

    # Option value (needed for theta and rho)
    V = black76_price(F, K, T, sigma, r, cp)

    # Theta (per calendar day)
    # d(V)/dT = disc * (F * nd1 * sigma / (2 * sqrt_T) - r * (V / disc))
    # But we want dV/dt where t is time elapsed, so dV/dT has opposite sign.
    # Theta per day = -dV/dT / 365
    time_decay = disc * F * nd1 * sigma / (2.0 * sqrt_T)
    if cp == "C":
        rate_component = r * disc * (F * Nd1 - K * Nd2)
    else:
        rate_component = r * disc * (F * (Nd1 - 1.0) - K * (Nd2 - 1.0))

    # dV/dT = time_decay - rate_component (positive dT → smaller value)
    # theta = -dV/dT per day
    theta = -(time_decay - rate_component) / 365.0

    # Vega — per 1% (0.01) move in vol
    vega = F * disc * nd1 * sqrt_T * 0.01

    # Rho — per 1% (0.01) move in r
    rho = -T * V * 0.01

    return GreeksResult(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def portfolio_greeks(
    positions: list[PositionGreeks],
    multiplier: float,
) -> AggregatedGreeks:
    """Aggregate Greeks across a portfolio of option positions.

    Args:
        positions:  List of PositionGreeks (qty can be negative for shorts).
        multiplier: Contract multiplier (e.g., 50 NTD per point for TXO).

    Returns:
        AggregatedGreeks with net delta (unitless), gamma, and NTD-scaled
        theta and vega.
    """
    net_delta = 0.0
    net_gamma = 0.0
    net_theta_ntd = 0.0
    net_vega_ntd = 0.0

    for pos in positions:
        q = pos.qty
        g = pos.greeks
        net_delta += q * g.delta
        net_gamma += q * g.gamma
        net_theta_ntd += q * g.theta * multiplier
        net_vega_ntd += q * g.vega * multiplier

    return AggregatedGreeks(
        net_delta=net_delta,
        net_gamma=net_gamma,
        net_theta_ntd=net_theta_ntd,
        net_vega_ntd=net_vega_ntd,
        positions=tuple(positions),
    )
