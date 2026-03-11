"""Fubon (富邦) contracts runtime: symbol validation and contract lookup."""

from __future__ import annotations

from typing import Any

from structlog import get_logger

logger = get_logger("feed_adapter.fubon.contracts_runtime")


class FubonContractsRuntime:
    """Manages Fubon contract lookup and symbol validation."""

    __slots__ = ("_sdk", "_contract_cache")

    def __init__(self, sdk: Any) -> None:
        self._sdk: Any = sdk
        self._contract_cache: dict[str, Any] = {}

    def validate_symbols(self, symbols: list[str]) -> list[str]:
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

    def get_contract(self, symbol: str) -> Any:
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]

        contract = self._lookup_from_sdk(symbol)
        if contract is not None:
            self._contract_cache[symbol] = contract
        return contract

    def _lookup_from_sdk(self, symbol: str) -> Any:
        if self._sdk is None:
            return None

        try:
            result = self._sdk.get_contract(symbol)
            if result is not None:
                return result
        except Exception as exc:
            logger.debug("Fubon contract lookup failed", symbol=symbol, error=str(exc))

        return None

    def clear_cache(self) -> None:
        self._contract_cache.clear()

    def cache_size(self) -> int:
        return len(self._contract_cache)
