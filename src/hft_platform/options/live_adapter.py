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

import structlog

from hft_platform.options.greeks import AggregatedGreeks, GreeksResult, PositionGreeks, portfolio_greeks
from hft_platform.options.surface import VolSurface

logger = structlog.get_logger(__name__)


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

        Builds a simulated position list by applying the intent's qty change
        to the matching symbol, then recomputes aggregated Greeks.
        """
        symbol = getattr(intent, "symbol", None)
        qty = getattr(intent, "qty", 0)
        side = getattr(intent, "side", None)

        if not symbol or qty == 0:
            return self.current_portfolio_greeks()

        # Determine signed qty change.
        # Side is an IntEnum: BUY=0, SELL=1. In Python 3.12, str(IntEnum)
        # returns "0"/"1", so the old "BUY" in str(side) check always failed
        # for IntEnum values, treating every intent as SELL.
        if side is None:
            signed_qty = qty
        elif isinstance(side, int):
            # IntEnum comparison: 0 = BUY, 1 = SELL
            signed_qty = qty if side == 0 else -qty
        else:
            # String fallback for tests or other callers
            signed_qty = qty if "BUY" in str(side).upper() else -qty

        # Look up per-contract Greeks from existing positions
        per_contract_greeks: GreeksResult | None = None
        for pos in self._positions:
            if pos.symbol == symbol:
                per_contract_greeks = pos.greeks
                break

        if per_contract_greeks is None:
            logger.warning(
                "simulated_greeks_unknown_symbol",
                symbol=symbol,
                msg="cannot simulate Greeks for unknown option; returning current portfolio",
            )
            return self.current_portfolio_greeks()

        # Build simulated positions: copy existing + adjust the target symbol's qty.
        # The `found` guard below is unreachable (per_contract_greeks is None when
        # symbol is absent, handled above), so we use a simple loop.
        simulated: list[PositionGreeks] = []
        for pos in self._positions:
            if pos.symbol == symbol:
                new_qty = pos.qty + signed_qty
                if new_qty != 0:
                    simulated.append(PositionGreeks(symbol=pos.symbol, qty=new_qty, greeks=pos.greeks))
            else:
                simulated.append(pos)

        return portfolio_greeks(simulated, self._multiplier)

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
        max_loss_ntd: float = -500_000.0,
    ) -> tuple[bool, float]:
        """Run scenario stress tests and return worst-case P&L.

        Args:
            scenarios:        List of ``ScenarioConfig`` objects.
            underlying_price: Current futures/underlying price.
            max_loss_ntd:     Maximum acceptable loss in NTD (negative).
                              Default: −500 000.

        Returns:
            ``(within_limit, worst_pnl_ntd)`` where ``within_limit`` is
            ``True`` when the worst scenario loss is above ``max_loss_ntd``.
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
        return (worst > max_loss_ntd, worst)
