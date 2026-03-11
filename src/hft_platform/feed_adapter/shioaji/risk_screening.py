from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.risk_screening")

_PUNISH_TTL_S: float = 300.0
_NOTICE_TTL_S: float = 300.0
_CREDIT_TTL_S: float = 60.0
_SHORT_STOCK_TTL_S: float = 60.0


class RiskScreeningGateway:
    """Risk screening queries: punish, notice, credit, short stock sources."""

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    def get_punish_stocks(self) -> Any:
        """Query disposition (punish) stocks list."""
        if self._client.mode == "simulation":
            return None
        cached = self._client._cache_get("punish")
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("punish"):
                return cached
            result = self._client.api.punish()
            self._client._record_api_latency("punish", start_ns, ok=True)
            self._client._cache_set("punish", _PUNISH_TTL_S, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("punish", start_ns, ok=False)
            logger.warning("Failed to fetch punish stocks", error=str(exc))
            return cached

    def get_notice_stocks(self) -> Any:
        """Query attention (notice) stocks list."""
        if self._client.mode == "simulation":
            return None
        cached = self._client._cache_get("notice")
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("notice"):
                return cached
            result = self._client.api.notice()
            self._client._record_api_latency("notice", start_ns, ok=True)
            self._client._cache_set("notice", _NOTICE_TTL_S, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("notice", start_ns, ok=False)
            logger.warning("Failed to fetch notice stocks", error=str(exc))
            return cached

    def get_credit_enquiries(self, contracts: list[Any]) -> list[Any]:
        """Query credit enquiries for given contracts."""
        if self._client.mode == "simulation":
            return []
        cached = self._client._cache_get("credit_enquiries")
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("credit_enquires"):
                return cached or []
            result = self._client.api.credit_enquires(contracts)
            self._client._record_api_latency("credit_enquires", start_ns, ok=True)
            self._client._cache_set("credit_enquiries", _CREDIT_TTL_S, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("credit_enquires", start_ns, ok=False)
            logger.warning("Failed to fetch credit enquiries", error=str(exc))
            return cached or []

    def get_short_stock_sources(self, contracts: list[Any]) -> list[Any]:
        """Query short stock sources for given contracts."""
        if self._client.mode == "simulation":
            return []
        cached = self._client._cache_get("short_stock_sources")
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("short_stock_sources"):
                return cached or []
            result = self._client.api.short_stock_sources(contracts)
            self._client._record_api_latency("short_stock_sources", start_ns, ok=True)
            self._client._cache_set("short_stock_sources", _SHORT_STOCK_TTL_S, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("short_stock_sources", start_ns, ok=False)
            logger.warning("Failed to fetch short stock sources", error=str(exc))
            return cached or []
