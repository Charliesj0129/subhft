"""Fubon (富邦) contracts runtime: symbol validation and contract lookup.

Mirrors the Shioaji ContractsRuntime pattern but adapted for fubon_neo SDK.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

logger = get_logger("feed_adapter.fubon.contracts_runtime")


class FubonContractsRuntime:
    """Manages Fubon contract lookup and symbol validation.

    The SDK instance is injected (typically from ``FubonSessionRuntime.sdk``).
    """

    __slots__ = ("_sdk", "_contract_cache")

    def __init__(self, sdk: Any) -> None:
        self._sdk: Any = sdk
        self._contract_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Symbol validation
    # ------------------------------------------------------------------ #

    def validate_symbols(self, symbols: list[str]) -> list[str]:
        """Return list of symbols that are valid (exist in broker contracts).

        Invalid symbols are logged as warnings and excluded from the result.
        """
        valid: list[str] = []
        invalid: list[str] = []
        for symbol in symbols:
            contract = self.get_contract(symbol)
            if contract is not None:
                valid.append(symbol)
            else:
                invalid.append(symbol)
        if invalid:
            logger.warning(
                "Fubon: invalid symbols excluded",
                count=len(invalid),
                symbols=invalid[:10],
            )
        return valid

    # ------------------------------------------------------------------ #
    # Contract lookup
    # ------------------------------------------------------------------ #

    def get_contract(self, symbol: str) -> Any:
        """Lookup a contract by symbol code.

        Checks local cache first, then queries the SDK.
        Returns None if the symbol is not found.
        """
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]

        contract = self._lookup_from_sdk(symbol)
        if contract is not None:
            self._contract_cache[symbol] = contract
        return contract

    def _lookup_from_sdk(self, symbol: str) -> Any:
        """Query the Fubon SDK for a contract by symbol.

        Uses ``sdk.get_contract(symbol)`` for lookup.
        Returns None if the symbol is not found or the SDK raises.
        """
        if self._sdk is None:
            return None

        # Try stock lookup
        try:
            result = self._sdk.get_contract(symbol)
            if result is not None:
                return result
        except Exception as exc:
            logger.debug("Fubon contract lookup failed", symbol=symbol, error=str(exc))

        return None

    # ------------------------------------------------------------------ #
    # Cache management
    # ------------------------------------------------------------------ #

    def clear_cache(self) -> None:
        """Clear the local contract cache."""
        self._contract_cache.clear()

    def cache_size(self) -> int:
        """Return number of cached contracts."""
        return len(self._contract_cache)
