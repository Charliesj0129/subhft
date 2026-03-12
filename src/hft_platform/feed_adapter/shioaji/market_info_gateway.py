from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.market_info_gateway")

# Cache TTLs in seconds
_CREDIT_CACHE_TTL_S = 60.0
_SHORT_STOCK_CACHE_TTL_S = 60.0
_PUNISH_CACHE_TTL_S = 300.0
_NOTICE_CACHE_TTL_S = 300.0


class MarketInfoGateway:
    """Dedicated gateway for market info queries (credit, short stock, punish, notice)."""

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    def _resolve_contracts(
        self, contract_codes: list[str], exchange: str, product_type: str | None = None
    ) -> list[Any]:
        """Resolve contract codes to Shioaji contract objects."""
        contracts: list[Any] = []
        for code in contract_codes:
            contract = self._client._get_contract(
                exchange, code, product_type=product_type, allow_synthetic=False
            )
            if contract:
                contracts.append(contract)
            else:
                logger.warning("Contract not found", code=code, exchange=exchange)
        return contracts

    def get_credit_enquires(
        self,
        contract_codes: list[str],
        exchange: str,
        timeout: int = 30000,
        product_type: str | None = None,
    ) -> list[Any]:
        """Query credit/margin trading info for stocks. Wraps api.credit_enquires()."""
        if self._client.mode == "simulation":
            return []
        cache_key = f"credit_enquires:{exchange}:{','.join(sorted(contract_codes))}"
        cached = self._client._cache_get(cache_key)
        if cached is not None:
            return cached
        if not self._client.api or not self._client.logged_in:
            return []
        contracts = self._resolve_contracts(contract_codes, exchange, product_type=product_type)
        if not contracts:
            logger.warning("No contracts resolved for credit_enquires")
            return []
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("credit_enquires"):
                return []
            result = self._client.api.credit_enquires(contracts, timeout=timeout)
            self._client._record_api_latency("credit_enquires", start_ns, ok=True)
            out = list(result) if result else []
            self._client._cache_set(cache_key, _CREDIT_CACHE_TTL_S, out)
            return out
        except Exception as exc:
            self._client._record_api_latency("credit_enquires", start_ns, ok=False)
            logger.warning("Failed to fetch credit_enquires", error=str(exc))
            return []

    def get_short_stock_sources(
        self,
        contract_codes: list[str],
        exchange: str,
        timeout: int = 5000,
        product_type: str | None = None,
    ) -> list[Any]:
        """Query available short stock sources. Wraps api.short_stock_sources()."""
        if self._client.mode == "simulation":
            return []
        cache_key = f"short_stock_sources:{exchange}:{','.join(sorted(contract_codes))}"
        cached = self._client._cache_get(cache_key)
        if cached is not None:
            return cached
        if not self._client.api or not self._client.logged_in:
            return []
        contracts = self._resolve_contracts(contract_codes, exchange, product_type=product_type)
        if not contracts:
            logger.warning("No contracts resolved for short_stock_sources")
            return []
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("short_stock_sources"):
                return []
            result = self._client.api.short_stock_sources(contracts, timeout=timeout)
            self._client._record_api_latency("short_stock_sources", start_ns, ok=True)
            out = list(result) if result else []
            self._client._cache_set(cache_key, _SHORT_STOCK_CACHE_TTL_S, out)
            return out
        except Exception as exc:
            self._client._record_api_latency("short_stock_sources", start_ns, ok=False)
            logger.warning("Failed to fetch short_stock_sources", error=str(exc))
            return []

    def get_punish_stocks(self, timeout: int = 5000) -> Any:
        """Get restricted/punished stocks list. Wraps api.punish()."""
        if self._client.mode == "simulation":
            return []
        cached = self._client._cache_get("punish_stocks")
        if cached is not None:
            return cached
        if not self._client.api or not self._client.logged_in:
            return []
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("punish_stocks"):
                return []
            result = self._client.api.punish(timeout=timeout)
            self._client._record_api_latency("punish_stocks", start_ns, ok=True)
            out = result if result is not None else []
            self._client._cache_set("punish_stocks", _PUNISH_CACHE_TTL_S, out)
            return out
        except Exception as exc:
            self._client._record_api_latency("punish_stocks", start_ns, ok=False)
            logger.warning("Failed to fetch punish stocks", error=str(exc))
            return []

    def get_notice_stocks(self, timeout: int = 5000) -> Any:
        """Get attention/notice stocks list. Wraps api.notice()."""
        if self._client.mode == "simulation":
            return []
        cached = self._client._cache_get("notice_stocks")
        if cached is not None:
            return cached
        if not self._client.api or not self._client.logged_in:
            return []
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("notice_stocks"):
                return []
            result = self._client.api.notice(timeout=timeout)
            self._client._record_api_latency("notice_stocks", start_ns, ok=True)
            out = result if result is not None else []
            self._client._cache_set("notice_stocks", _NOTICE_CACHE_TTL_S, out)
            return out
        except Exception as exc:
            self._client._record_api_latency("notice_stocks", start_ns, ok=False)
            logger.warning("Failed to fetch notice stocks", error=str(exc))
            return []
