"""ContractLifecycleManager — automatic futures rollover and option chain updates."""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

import structlog

from hft_platform.notifications.alert import Alert, AlertSeverity

logger = structlog.get_logger("ops.contract_lifecycle")


def _make_id() -> str:
    import uuid

    return str(uuid.uuid4())[:8]


class ContractLifecycleManager:
    """Manages contract expiry detection, futures alias refresh, and option chain updates."""

    __slots__ = (
        "_contracts_runtime",
        "_alert_callback",
        "_expiry_warn_days",
        "_option_strike_range",
        "_known_expiries",
        "_warned_expiries",
    )

    def __init__(
        self,
        contracts_runtime: Any,
        alert_callback: Callable[[Alert], Awaitable[None]],
        expiry_warn_days: list[int] | None = None,
        option_strike_range: int = 10,
    ) -> None:
        self._contracts_runtime = contracts_runtime
        self._alert_callback = alert_callback
        self._expiry_warn_days = sorted(expiry_warn_days or [3, 1], reverse=True)
        self._option_strike_range = option_strike_range
        self._known_expiries: dict[str, date] = {}
        self._warned_expiries: set[tuple[str, int]] = set()

    async def check_expiries(self, today: date) -> None:
        """Check all registered contracts for upcoming expiry and fire alerts."""
        from hft_platform.core import timebase

        for symbol, expiry in self._known_expiries.items():
            days_until = (expiry - today).days
            for warn_days in self._expiry_warn_days:
                key = (symbol, warn_days)
                if days_until == warn_days and key not in self._warned_expiries:
                    severity = AlertSeverity.WARN if warn_days <= 1 else AlertSeverity.INFO
                    alert = Alert(
                        alert_id=_make_id(),
                        severity=severity,
                        category="contract",
                        source="contract_lifecycle",
                        title=f"Contract {symbol} expires in {days_until} days",
                        detail=f"Contract {symbol} expires on {expiry}. Days remaining: {days_until}.",
                        ts_ns=timebase.now_ns(),
                        dedup_key=f"expiry:{symbol}:{warn_days}",
                        metadata={"symbol": symbol, "expiry": str(expiry), "days_until": days_until},
                    )
                    await self._alert_callback(alert)
                    self._warned_expiries.add(key)
                    break

    async def refresh_futures_aliases(self) -> dict[str, str]:
        """Refresh the contract cache and return updated futures alias mappings."""
        await self._contracts_runtime.refresh_contract_cache()
        alias_map = self._contracts_runtime.resolve_symbol_aliases()
        logger.info("contract_lifecycle.futures_aliases_refreshed", aliases=alias_map)
        return alias_map

    async def refresh_option_chain(self, underlying_price: int = 0) -> list[Any]:
        """Fetch the current option chain contracts from the contracts runtime."""
        contracts = await self._contracts_runtime.get_option_contracts()
        logger.info(
            "contract_lifecycle.option_chain_refreshed",
            count=len(contracts),
            underlying_price=underlying_price,
        )
        return contracts

    def register_expiry(self, symbol: str, expiry: date) -> None:
        """Register a contract symbol and its expiry date for monitoring."""
        self._known_expiries[symbol] = expiry
