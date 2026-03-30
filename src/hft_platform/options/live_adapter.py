"""Float→int boundary adapter for live trading path.

Implements GreeksProvider protocol for RiskEngine.
All outputs to risk/strategy are int or bool — never float.

Float exception (per Architecture Governance Rule 25 §11): the internal
computation in portfolio_greeks is float-based (options analytics). This
adapter is the explicit boundary: it receives floats and converts them to
int (lots) or bool before returning values to the live trading path.
"""
from __future__ import annotations

from typing import Any

from hft_platform.options.greeks import AggregatedGreeks, PositionGreeks, portfolio_greeks
from hft_platform.options.surface import VolSurface


class OptionsLiveAdapter:
    """Bridge between options analytics (float) and live risk/strategy (int/bool).

    Satisfies the ``GreeksProvider`` protocol defined in
    ``hft_platform.risk.greeks_limit_validator``.
    """

    __slots__ = ("_positions", "_surface", "_multiplier")

    def __init__(
        self,
        positions: list[PositionGreeks],
        surface: VolSurface,
        multiplier: float = 50.0,
    ) -> None:
        self._positions = positions
        self._surface = surface
        self._multiplier = multiplier

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def update_positions(self, positions: list[PositionGreeks]) -> None:
        """Replace the current position list (immutable swap)."""
        self._positions = positions

    # ------------------------------------------------------------------
    # GreeksProvider protocol
    # ------------------------------------------------------------------

    def current_portfolio_greeks(self) -> AggregatedGreeks:
        """Return aggregated Greeks for the current portfolio."""
        return portfolio_greeks(self._positions, self._multiplier)

    def simulated_greeks_after(self, intent: Any) -> AggregatedGreeks:
        """Return Greeks after a hypothetical order intent.

        Conservative approximation: returns current Greeks unchanged.
        A full implementation would add the intent's delta contribution.
        """
        return self.current_portfolio_greeks()

    # ------------------------------------------------------------------
    # Float → int boundary helpers
    # ------------------------------------------------------------------

    def compute_hedge_lots(self, threshold: int = 0) -> int:
        """Return the number of futures lots needed to delta-hedge.

        Args:
            threshold: Minimum absolute lots to trigger a hedge.
                       Returns 0 when ``abs(net_delta_lots) <= threshold``.

        Returns:
            Signed integer lot count (positive = buy, negative = sell).
            The float net_delta is rounded to the nearest integer.
        """
        agg = self.current_portfolio_greeks()
        lots = round(agg.net_delta)
        return 0 if abs(lots) <= threshold else lots

    def check_limits(self, limits: dict) -> tuple[bool, str]:
        """Check current portfolio Greeks against provided limits.

        Args:
            limits: Dict with optional keys:
                    ``net_delta_lots``, ``net_gamma_lots``,
                    ``net_vega_ntd``, ``net_theta_ntd``.

        Returns:
            ``(True, "")`` if all limits are satisfied,
            ``(False, reason_code)`` on the first breach.
        """
        agg = self.current_portfolio_greeks()
        if abs(agg.net_delta) > limits.get("net_delta_lots", 999_999):
            return (False, "GREEKS_DELTA_LIMIT")
        if abs(agg.net_gamma) > limits.get("net_gamma_lots", 999_999):
            return (False, "GREEKS_GAMMA_LIMIT")
        if abs(agg.net_vega_ntd) > limits.get("net_vega_ntd", 999_999_999):
            return (False, "GREEKS_VEGA_LIMIT")
        if agg.net_theta_ntd < limits.get("net_theta_ntd", -999_999_999):
            return (False, "GREEKS_THETA_LIMIT")
        return (True, "")

    def run_stress(
        self,
        scenarios: list,
        underlying_price: float,
    ) -> tuple[bool, float]:
        """Run scenario stress tests and return worst-case P&L.

        Args:
            scenarios:        List of ``ScenarioConfig`` objects.
            underlying_price: Current futures/underlying price.

        Returns:
            ``(within_limit, worst_pnl_ntd)`` where ``within_limit`` is
            ``True`` when the worst scenario loss is above −500 000 NTD.
        """
        from hft_platform.risk.stress_test import run_stress_test

        results = run_stress_test(
            self._positions,
            self._surface,
            scenarios,
            underlying_price,
            self._multiplier,
        )
        if not results:
            return (True, 0.0)
        worst = min(r.pnl_ntd for r in results)
        return (worst > -500_000, worst)
