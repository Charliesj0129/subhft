"""Post-validator Greeks limit check for RiskEngine."""

from __future__ import annotations

from typing import Any, Protocol

from structlog import get_logger

from hft_platform.options.greeks import AggregatedGreeks

logger = get_logger("risk.greeks_limit")


class GreeksProvider(Protocol):
    def current_portfolio_greeks(self) -> AggregatedGreeks: ...
    def simulated_greeks_after(self, intent: Any) -> AggregatedGreeks: ...


class GreeksLimitValidator:
    __slots__ = ("_limits", "_provider", "_enabled")

    def __init__(self, config: dict, greeks_provider: GreeksProvider | None) -> None:
        self._limits = config.get("greeks_limits", {})
        self._provider = greeks_provider
        self._enabled = bool(self._limits.get("enabled", False))

    def check(self, intent: Any) -> tuple[bool, str]:
        if not self._enabled or self._provider is None:
            return (True, "")
        try:
            sim = self._provider.simulated_greeks_after(intent)
        except Exception as exc:
            logger.warning("greeks_provider_error", error=str(exc))
            return (True, "")
        if abs(sim.net_delta) > self._limits.get("net_delta_lots", 999999):
            return (False, "GREEKS_DELTA_LIMIT")
        if abs(sim.net_gamma) > self._limits.get("net_gamma_lots", 999999):
            return (False, "GREEKS_GAMMA_LIMIT")
        if abs(sim.net_vega_ntd) > self._limits.get("net_vega_ntd", 999999999):
            return (False, "GREEKS_VEGA_LIMIT")
        if sim.net_theta_ntd < self._limits.get("net_theta_ntd", -999999999):
            return (False, "GREEKS_THETA_LIMIT")
        return (True, "")
