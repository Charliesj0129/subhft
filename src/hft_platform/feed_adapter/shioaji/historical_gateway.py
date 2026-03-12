from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.historical_gateway")

_VALID_TICKS_QUERY_TYPES = frozenset({"AllDay", "RangeTime", "LastCount"})


class HistoricalGateway:
    """Dedicated historical data query gateway (ticks / kbars)."""

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    @staticmethod
    def _sdk() -> Any | None:
        try:
            from hft_platform.feed_adapter import shioaji_client as client_module

            return getattr(client_module, "sj", None)
        except Exception:
            return None

    def _resolve_query_type(self, query_type: str) -> Any:
        """Map query_type string to SDK TicksQueryType constant."""
        sdk = self._sdk()
        if sdk is None:
            raise RuntimeError("Shioaji SDK unavailable")
        if query_type not in _VALID_TICKS_QUERY_TYPES:
            raise ValueError(f"Unknown query_type {query_type!r}; expected one of {sorted(_VALID_TICKS_QUERY_TYPES)}")
        ticks_qt = getattr(sdk.constant, "TicksQueryType", None)
        if ticks_qt is None:
            raise RuntimeError("Shioaji SDK missing TicksQueryType constant")
        return getattr(ticks_qt, query_type)

    def get_ticks(
        self,
        contract_code: str,
        exchange: str,
        date: str,
        query_type: str = "AllDay",
        time_start: str | None = None,
        time_end: str | None = None,
        last_cnt: int | None = None,
        timeout: int = 30000,
        product_type: str | None = None,
    ) -> Any:
        """Fetch historical tick data. Wraps ``api.ticks()``."""
        if not self._client.api or not self._client.logged_in:
            logger.warning("API not available; cannot fetch ticks")
            return None

        if not self._client._rate_limit_api("ticks"):
            logger.warning("Rate limit exceeded for ticks")
            return None

        contract = self._client._get_contract(
            exchange,
            contract_code,
            product_type=product_type,
            allow_synthetic=False,
        )
        if not contract:
            raise ValueError(f"Contract {contract_code} not found on {exchange}")

        qt = self._resolve_query_type(query_type)

        kwargs: dict[str, Any] = {
            "contract": contract,
            "date": date,
            "query_type": qt,
            "timeout": timeout,
        }
        if time_start is not None:
            kwargs["time_start"] = time_start
        if time_end is not None:
            kwargs["time_end"] = time_end
        if last_cnt is not None:
            kwargs["last_cnt"] = last_cnt

        start_ns = time.perf_counter_ns()
        try:
            result = self._client.api.ticks(**kwargs)
            self._client._record_api_latency("ticks", start_ns, ok=True)
            return result
        except Exception as exc:
            self._client._record_api_latency("ticks", start_ns, ok=False)
            logger.error("get_ticks failed", error=str(exc))
            raise

    def get_kbars(
        self,
        contract_code: str,
        exchange: str,
        start: str,
        end: str,
        timeout: int = 30000,
        product_type: str | None = None,
    ) -> Any:
        """Fetch historical K-bar data. Wraps ``api.kbars()``."""
        if not self._client.api or not self._client.logged_in:
            logger.warning("API not available; cannot fetch kbars")
            return None

        if not self._client._rate_limit_api("kbars"):
            logger.warning("Rate limit exceeded for kbars")
            return None

        contract = self._client._get_contract(
            exchange,
            contract_code,
            product_type=product_type,
            allow_synthetic=False,
        )
        if not contract:
            raise ValueError(f"Contract {contract_code} not found on {exchange}")

        start_ns = time.perf_counter_ns()
        try:
            result = self._client.api.kbars(
                contract=contract,
                start=start,
                end=end,
                timeout=timeout,
            )
            self._client._record_api_latency("kbars", start_ns, ok=True)
            return result
        except Exception as exc:
            self._client._record_api_latency("kbars", start_ns, ok=False)
            logger.error("get_kbars failed", error=str(exc))
            raise
