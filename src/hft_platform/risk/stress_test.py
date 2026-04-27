"""Scenario-based portfolio stress testing using Greeks-based P&L approximation.

Uses the second-order Taylor expansion:
    ΔP ≈ δ·ΔS + ½γ·ΔS² + ν·Δσ

where:
    δ  = delta
    γ  = gamma
    ΔS = change in underlying price
    ν  = vega (per 1% vol move, i.e. per 0.01 sigma)
    Δσ = change in implied vol (absolute, e.g. 0.05 = +5 vol points)

Float exception: Per Architecture Governance Rule 25 §11, float is permitted
in this module — it is an offline analytics component (not a live trading path).
"""

from __future__ import annotations

from dataclasses import dataclass

from hft_platform.options.greeks import GreeksResult, PositionGreeks
from hft_platform.options.surface import VolSurface


@dataclass(slots=True)
class ScenarioConfig:
    """Definition of a single stress scenario."""

    name: str
    underlying_shift_pct: float  # e.g. -3.0 means −3% move
    vol_shift_abs: float  # e.g.  0.05 means +5 vol points (absolute)


@dataclass(slots=True)
class ScenarioResult:
    """P&L outcome and post-scenario Greek snapshot for one scenario."""

    name: str
    underlying_shift_pct: float
    vol_shift_abs: float
    pnl_ntd: float  # NTD P&L across all positions
    greeks_after: list[PositionGreeks]  # approximate Greeks post-shock


def _apply_scenario(
    positions: list[PositionGreeks],
    underlying_price: float,
    multiplier: float,
    scenario: ScenarioConfig,
) -> ScenarioResult:
    """Compute P&L and post-shock Greeks for *scenario*."""
    delta_s = underlying_price * scenario.underlying_shift_pct / 100.0
    delta_sigma = scenario.vol_shift_abs  # already absolute vol shift

    total_pnl = 0.0
    greeks_after: list[PositionGreeks] = []

    for pos in positions:
        g = pos.greeks
        # Second-order price approximation
        # Vega is defined per 1% (0.01) vol move; delta_sigma is in absolute
        # vol units, so we scale by 100 to convert to "% vol" units.
        price_change = g.delta * delta_s + 0.5 * g.gamma * delta_s * delta_s + g.vega * delta_sigma * 100.0
        total_pnl += pos.qty * price_change * multiplier

        # Approximate post-shock delta using delta + gamma * ΔS
        new_delta = g.delta + g.gamma * delta_s
        new_greeks = GreeksResult(
            delta=new_delta,
            gamma=g.gamma,
            theta=g.theta,
            vega=g.vega,
            rho=g.rho,
        )
        greeks_after.append(PositionGreeks(symbol=pos.symbol, qty=pos.qty, greeks=new_greeks))

    return ScenarioResult(
        name=scenario.name,
        underlying_shift_pct=scenario.underlying_shift_pct,
        vol_shift_abs=scenario.vol_shift_abs,
        pnl_ntd=total_pnl,
        greeks_after=greeks_after,
    )


def run_stress_test(
    positions: list[PositionGreeks],
    surface: VolSurface,
    scenarios: list[ScenarioConfig],
    underlying_price: float,
    multiplier: float,
    risk_free_rate: float = 0.01,
) -> list[ScenarioResult]:
    """Run a set of stress scenarios over a portfolio of option positions.

    Args:
        positions:       List of option positions with pre-computed Greeks.
        surface:         Current implied volatility surface (used for context;
                         IV lookups are not re-priced in this approximation).
        scenarios:       List of scenario definitions to evaluate.
        underlying_price: Current futures/underlying price.
        multiplier:      Contract multiplier in NTD per point (e.g. 50 for TXO).
        risk_free_rate:  Risk-free rate (unused in approximation; reserved for
                         full re-pricing extensions).

    Returns:
        List of ScenarioResult, one per scenario, in the same order as *scenarios*.
    """
    # P3-?: vulture flagged `surface` and `risk_free_rate` as 100% unused; both
    # are documented public-API parameters reserved for the future full
    # re-pricing extension (cf. docstring above). Tests pass them consistently,
    # so the right fix is to mark them used here — `del`-ing them is a no-op at
    # runtime and silences vulture without breaking callers. When the
    # re-pricing extension lands, replace the `del` lines with the actual
    # consumers (IV lookup via `surface`, discount factor via `risk_free_rate`).
    del surface  # reserved for IV lookup in future re-pricing
    del risk_free_rate  # reserved for discount factor in future re-pricing
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        if not positions:
            results.append(
                ScenarioResult(
                    name=scenario.name,
                    underlying_shift_pct=scenario.underlying_shift_pct,
                    vol_shift_abs=scenario.vol_shift_abs,
                    pnl_ntd=0.0,
                    greeks_after=[],
                )
            )
        else:
            results.append(_apply_scenario(positions, underlying_price, multiplier, scenario))
    return results
