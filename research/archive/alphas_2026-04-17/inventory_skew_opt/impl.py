"""Riccati-Optimal Inventory Skew for OpportunisticMM.

Based on Barzykin (2603.07752) adiabatic quadratic approximation,
adapted for CLOB (removing OTC-specific RFQ/rejection/reputation mechanics).

CLOB-Compatible Derivation
--------------------------
Starting from the full Barzykin model (Section 2), we strip:
  - Trade rejection control (ell): CLOB has no last-look; all fills execute
  - Rejection score R: no reputation variable (CLOB is anonymous)
  - Size ladder {z_n}: we trade unit size (z=1) per OpMM design
  - Quote-dependent adverse selection m_n(delta): CLOB fills are at posted price

What survives (Barzykin Eq. 26-28, 38):
  1. Quadratic value function ansatz: V(t,q) = -A(t)*q^2 - C(t)
  2. Riccati ODE: A'(t) + gamma*sigma^2/2 = 4*g*A(t)^2 * Sigma
  3. Stationary solution: A = sqrt(gamma*sigma^2 / (8*g*Sigma))
  4. Optimal quote: delta*(q) = 1/kappa + A*(z +/- 2q)
  5. Skew component: skew(q) = 2*A*q (symmetric around q=0)

This is actually the standard Avellaneda-Stoikov / Gueant et al. (2013) result.
The key insight: the optimal skew IS already quadratic (linear in q),
so at low inventory the Riccati solution matches the current linear skew.

The question is: are the COEFFICIENTS different?

Current SimpleMarketMaker linear skew:
  skew_x2 = -(pos * tick_size * 2) / INVENTORY_SKEW_DIVISOR
  => skew_per_contract = tick_size / 5

Riccati optimal (stationary):
  skew_per_contract = 2*A = sqrt(gamma*sigma^2 / (2*g*Sigma))

The comparison depends on calibrated parameters (gamma, sigma, kappa, Lambda).

References
----------
- Barzykin (2603.07752): Sections 2.1-2.3, Eq. 26-38
- Avellaneda & Stoikov (2008): Original HFT MM optimal control
- Gueant, Lehalle, Fernandez-Tapia (2013): Inventory penalization solution
- Chavez-Casillas et al. (2405.11444): Adds state-dependent fill rates
  (not implemented here due to fill probability estimation requirements)

Precision Law compliance:
  - All prices in scaled integers (x10000) in production
  - Research code uses float for mathematical derivations
  - Production integration would convert final skew to scaled int
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------------------
# Riccati ODE solver
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MarketParams:
    """Calibrated market parameters for the AS/Barzykin model.

    All values in natural units (not scaled).
    """

    sigma: float          # price volatility (points per sqrt(second))
    gamma: float          # risk aversion coefficient (1/points)
    kappa: float          # intensity decay parameter (1/points)
    lambda_0: float       # base arrival rate (fills per second)
    T: float              # trading horizon (seconds)
    tick_size: float = 1.0  # minimum price increment (points)


@dataclass(frozen=True, slots=True)
class RiccatiSolution:
    """Solution of the Riccati ODE for the inventory penalty coefficient."""

    A_stationary: float    # stationary (long-horizon) value of A
    A_t: np.ndarray        # time-varying A(t) over the horizon
    t_grid: np.ndarray     # time grid for A(t)
    half_spread_base: float  # base half-spread (1/kappa component)
    skew_per_unit: float   # inventory skew per unit: 2*A


def solve_riccati(params: MarketParams, n_steps: int = 1000) -> RiccatiSolution:
    """Solve the Riccati ODE for the inventory penalty coefficient A(t).

    From Barzykin Eq. 27-28 (with g=1, no reputation):
      A'(t) + gamma*sigma^2/2 = 4*A(t)^2 * Sigma
    where
      Sigma = Lambda_0 * exp(-kappa * delta_bar) * kappa  (Eq. 36-37 simplified)

    For the stationary solution (A' = 0):
      A = sqrt(gamma * sigma^2 / (8 * Sigma))

    Terminal condition: A(T) = 0 (no inventory penalty at horizon end)
    The ODE is solved backwards from T to 0.
    """
    # Compute Sigma (Hamiltonian second derivative at p=0)
    # From Gueant et al. (2013): H''(0) = Lambda * kappa * Phi(mu_tilde)
    # In the simplified case (no rejection, no adverse selection):
    # delta_bar = 1/kappa + gamma*sigma^2/(2*kappa*Lambda_0)  (myopic optimizer)
    # Sigma = Lambda_0 * exp(-kappa * delta_bar) * kappa
    #
    # For simplicity, use the Gueant (2013) closed-form:
    # A_stationary = gamma * sigma^2 / (2 * kappa * Lambda_0)  (simplified)
    #
    # More precisely, from the FOC at p=0:
    # delta_bar = 1/kappa (when A is small, which is our regime)
    # So Sigma = Lambda_0 * exp(-1) * kappa

    sigma = params.sigma
    gamma = params.gamma
    kappa = params.kappa
    lambda_0 = params.lambda_0

    # Hamiltonian curvature (Barzykin Eq. 36, simplified for z=1, no rejection)
    delta_bar = 1.0 / kappa  # myopic optimal spread at p=0
    sigma_h = lambda_0 * math.exp(-kappa * delta_bar) * kappa
    # sigma_h = lambda_0 * exp(-1) * kappa

    # Stationary solution (Barzykin Eq. 28)
    if sigma_h > 0:
        A_stat = math.sqrt(gamma * sigma**2 / (8.0 * sigma_h))
    else:
        A_stat = 0.0

    # Solve ODE backwards: A'(t) = 4*Sigma*A^2 - gamma*sigma^2/2
    # with A(T) = 0
    dt = params.T / n_steps
    t_grid = np.linspace(0, params.T, n_steps + 1)
    A_t = np.zeros(n_steps + 1)

    # Backward integration (RK4)
    c1 = 4.0 * sigma_h
    c2 = 0.5 * gamma * sigma**2

    def f_riccati(a: float) -> float:
        return c1 * a**2 - c2

    for i in range(n_steps - 1, -1, -1):
        a = A_t[i + 1]
        # RK4 step (backward, so negative dt)
        # Clamp A to prevent overflow (A should converge to A_stat)
        k1 = -dt * f_riccati(a)
        a1 = a + 0.5 * k1
        if abs(a1) > 10 * A_stat + 100:
            A_t[i] = A_stat
            continue
        k2 = -dt * f_riccati(a1)
        a2 = a + 0.5 * k2
        if abs(a2) > 10 * A_stat + 100:
            A_t[i] = A_stat
            continue
        k3 = -dt * f_riccati(a2)
        a3 = a + k3
        if abs(a3) > 10 * A_stat + 100:
            A_t[i] = A_stat
            continue
        k4 = -dt * f_riccati(a3)
        A_t[i] = a + (k1 + 2 * k2 + 2 * k3 + k4) / 6.0
        # Clamp to stationary value (backward integration can diverge)
        if A_t[i] > 2.0 * A_stat:
            A_t[i] = A_stat

    return RiccatiSolution(
        A_stationary=A_stat,
        A_t=A_t,
        t_grid=t_grid,
        half_spread_base=1.0 / kappa,
        skew_per_unit=2.0 * A_stat,
    )


# ---------------------------------------------------------------------------
# Skew comparison: linear vs Riccati
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SkewComparison:
    """Comparison of linear vs Riccati skew at a given inventory level."""

    inventory: int
    linear_skew_pts: float       # current linear skew (points)
    riccati_skew_pts: float      # Riccati optimal skew (points)
    difference_pts: float        # riccati - linear
    difference_bps: float        # difference in bps of mid price
    linear_half_spread: float    # total half-spread with linear skew
    riccati_half_spread: float   # total half-spread with Riccati skew


def compare_skews(
    params: MarketParams,
    solution: RiccatiSolution,
    *,
    mid_price: float = 22000.0,  # TXFD6 mid price in points
    inventories: list[int] | None = None,
    tick_size_ratio_pct: int = 50,
    inventory_divisor: int = 5,
) -> list[SkewComparison]:
    """Compare linear vs Riccati skew at various inventory levels.

    Linear skew (current SimpleMarketMaker):
      skew = (inventory * tick_size * tick_size_ratio_pct / 100) / inventory_divisor

    Riccati skew (optimal):
      skew = 2 * A_stationary * inventory
    """
    if inventories is None:
        inventories = list(range(0, 11))

    tick_size = params.tick_size
    # Current linear: tick_size_scaled = spread * tick_size_ratio_pct / 100
    # For TXFD6, typical spread ~ 5 points, so tick_size_scaled ~ 2.5 points
    typical_spread = 5.0  # points
    tick_size_scaled = typical_spread * tick_size_ratio_pct / 100.0
    linear_coeff = tick_size_scaled / inventory_divisor  # points per unit

    riccati_coeff = solution.skew_per_unit  # points per unit

    results = []
    for q in inventories:
        linear_skew = linear_coeff * q
        riccati_skew = riccati_coeff * q

        diff_pts = riccati_skew - linear_skew
        diff_bps = diff_pts / mid_price * 10000.0 if mid_price > 0 else 0.0

        # Half-spread = base + skew
        base_half = max(tick_size_scaled, typical_spread / 2.0)

        results.append(
            SkewComparison(
                inventory=q,
                linear_skew_pts=linear_skew,
                riccati_skew_pts=riccati_skew,
                difference_pts=diff_pts,
                difference_bps=diff_bps,
                linear_half_spread=base_half + linear_skew,
                riccati_half_spread=base_half + riccati_skew,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Parameter calibration from TXFD6 data
# ---------------------------------------------------------------------------

def calibrate_from_txfd6(
    data: np.ndarray,
    *,
    session_hours: float = 5.0,
) -> MarketParams:
    """Calibrate model parameters from TXFD6 L1 data.

    Parameters estimated:
    - sigma: price volatility from tick-to-tick returns
    - kappa: fill intensity decay from spread-fill relationship
    - lambda_0: base fill rate from tick frequency
    - gamma: risk aversion (set to reasonable default)

    Args:
        data: L1 structured array with bid_px, ask_px, local_ts fields
        session_hours: trading session length in hours
    """
    mid_prices = data["mid_price"]
    timestamps = data["local_ts"]
    spreads_bps = data["spread_bps"]

    n = len(data)

    # 1. Volatility: annualized from tick-to-tick returns
    dt_ns = np.diff(timestamps).astype(np.float64)
    dt_s = dt_ns / 1e9
    # Filter out unreasonable gaps (> 10 seconds)
    valid = dt_s < 10.0
    returns = np.diff(mid_prices)[valid]
    dt_valid = dt_s[valid]

    if len(returns) < 100:
        # Fallback defaults for TXFD6
        return MarketParams(
            sigma=2.0,      # ~2 pts/sqrt(s)
            gamma=0.001,    # moderate risk aversion
            kappa=2.0,      # 1/kappa ~ 0.5 pts half-spread
            lambda_0=8.0,   # ~8 ticks/second
            T=session_hours * 3600.0,
            tick_size=1.0,
        )

    # Variance per second
    var_per_tick = np.var(returns)
    median_dt = float(np.median(dt_valid))
    sigma = math.sqrt(var_per_tick / median_dt) if median_dt > 0 else 2.0

    # 2. Fill rate: ticks per second as proxy
    total_time_s = float(timestamps[-1] - timestamps[0]) / 1e9
    lambda_0 = n / total_time_s if total_time_s > 0 else 8.0

    # 3. Kappa: intensity decay parameter
    # In the AS model, the optimal half-spread ~ 1/kappa + inventory_term.
    # We calibrate kappa so that 1/kappa ~ typical half-spread in points.
    # TXFD6: typical spread ~ 5 pts, so half-spread ~ 2.5 pts => kappa ~ 0.4
    avg_mid = float(np.mean(mid_prices))
    spread_pts = float(np.median(spreads_bps)) * avg_mid / 10000.0
    half_spread_pts = spread_pts / 2.0
    kappa = 1.0 / max(half_spread_pts, 0.5)

    # 4. Gamma: risk aversion
    # Calibrate so that the Riccati skew at q=5 ~ spread/2.
    # From A_stat = sqrt(gamma*sigma^2 / (8*Sigma)), skew@q=5 = 10*A_stat.
    # We want 10*A_stat ~ half_spread_pts.
    # Sigma ~ lambda_0 * exp(-1) * kappa
    sigma_h = lambda_0 * math.exp(-1.0) * kappa
    # A_target = half_spread_pts / 10
    # A_target^2 = gamma*sigma^2 / (8*Sigma)
    # gamma = A_target^2 * 8 * Sigma / sigma^2
    A_target = half_spread_pts / 10.0
    gamma = (A_target**2 * 8.0 * sigma_h / (sigma**2)) if sigma > 0 else 0.001

    return MarketParams(
        sigma=sigma,
        gamma=gamma,
        kappa=kappa,
        lambda_0=lambda_0,
        T=session_hours * 3600.0,
        tick_size=1.0,
    )


# ---------------------------------------------------------------------------
# Production integration point
# ---------------------------------------------------------------------------

class RiccatiSkewCalculator:
    """Drop-in replacement for linear skew in SimpleMarketMaker.

    Integration path:
        In SimpleMarketMaker.on_stats(), replace:

            # OLD: linear skew
            skew_x2 = -(pos * tick_size_scaled * 2) // self.INVENTORY_SKEW_DIVISOR

            # NEW: Riccati optimal skew
            skew_x2 = self._riccati_skew.compute_skew_x2(pos, tick_size_scaled)

    Uses pre-computed A coefficient (no allocation on hot path).
    """

    __slots__ = ("_A", "_scale_factor")

    def __init__(self, A_stationary: float, price_scale: int = 10_000) -> None:
        """Initialize with pre-computed Riccati coefficient.

        Args:
            A_stationary: the inventory penalty coefficient from Riccati ODE
            price_scale: price scaling factor (10000 for our convention)
        """
        self._A = A_stationary
        self._scale_factor = price_scale

    def compute_skew_x2(self, position: int, tick_size_scaled: int) -> int:
        """Compute optimal inventory skew in x2 scaled-int units.

        Returns value compatible with SimpleMarketMaker's fair_value_x2
        computation (negative for long inventory, positive for short).

        Hot-path safe: no allocation, pure arithmetic.
        """
        # Riccati skew: -2*A*q in price units
        # In scaled units: -2*A*q * scale_factor
        # In x2 units: multiply by 2
        skew_x2 = -int(4.0 * self._A * position * self._scale_factor)
        return skew_x2

    @classmethod
    def from_params(cls, params: MarketParams) -> "RiccatiSkewCalculator":
        """Create from market parameters."""
        solution = solve_riccati(params)
        return cls(A_stationary=solution.A_stationary)
