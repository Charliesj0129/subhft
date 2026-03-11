from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

try:
    import shioaji as sj
except Exception:
    sj = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.data_gateway")


class DataGateway:
    """Historical data queries: ticks, kbars, snapshots."""

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Ticks
    # ------------------------------------------------------------------

    def get_ticks(
        self,
        contract: Any,
        date: str,
        query_type: str = "AllDay",
        time_start: str | None = None,
        time_end: str | None = None,
        last_cnt: int | None = None,
        timeout: int = 30000,
    ) -> Any:
        """Fetch historical tick data for *contract* on *date*.

        ``query_type`` is mapped to ``sj.constant.TicksQueryType`` by name;
        defaults to ``AllDay`` when the name is unrecognised.
        """
        if not self._client.api or not self._client.logged_in:
            logger.warning("API not available; skipping get_ticks")
            return None
        if not self._client._rate_limit_api("ticks"):
            logger.warning("Rate limited; skipping get_ticks")
            return None

        query_type_enum = self._resolve_ticks_query_type(query_type)

        kwargs: dict[str, Any] = {
            "contract": contract,
            "date": date,
            "query_type": query_type_enum,
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
            return None

    # ------------------------------------------------------------------
    # Kbars
    # ------------------------------------------------------------------

    def get_kbars(
        self,
        contract: Any,
        start: str,
        end: str,
        timeout: int = 30000,
    ) -> Any:
        """Fetch K-bar (OHLCV) data for *contract* between *start* and *end*."""
        if not self._client.api or not self._client.logged_in:
            logger.warning("API not available; skipping get_kbars")
            return None
        if not self._client._rate_limit_api("kbars"):
            logger.warning("Rate limited; skipping get_kbars")
            return None

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
            return None

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def get_snapshots(self, contracts: list[Any] | None = None) -> list[Any]:
        """Fetch snapshots, optionally resolving contracts from client symbols.

        When *contracts* is ``None`` the method resolves tradeable contracts
        from ``self._client.symbols`` (same logic previously in
        ``AccountGateway.fetch_snapshots``).
        """
        if not self._client.api or not self._client.logged_in:
            logger.info("API not available; skipping snapshot fetch")
            return []

        resolved = contracts if contracts is not None else self._resolve_symbol_contracts()
        if not resolved:
            logger.warning("No contracts resolved for snapshots")
            return []

        if not self._client._rate_limit_api("snapshots"):
            logger.warning("Rate limited; skipping snapshots")
            return []

        snapshots: list[Any] = []
        batch_size = 500
        for i in range(0, len(resolved), batch_size):
            batch = resolved[i : i + batch_size]
            logger.info("Requesting snapshots", batch_size=len(batch))
            start_ns = time.perf_counter_ns()
            try:
                results = self._client.api.snapshots(batch)
                self._client._record_api_latency("snapshots", start_ns, ok=True)
                snapshots.extend(results or [])
                time.sleep(0.11)
            except Exception as exc:
                self._client._record_api_latency("snapshots", start_ns, ok=False)
                logger.error("Snapshot fetch failed", error=str(exc))
        return snapshots

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_symbol_contracts(self) -> list[Any]:
        """Resolve contracts from ``self._client.symbols``."""
        contracts: list[Any] = []
        for sym in self._client.symbols:
            code = sym.get("code")
            exchange = sym.get("exchange")
            product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
            if not code or not exchange:
                continue
            contract = self._client._get_contract(exchange, code, product_type=product_type, allow_synthetic=False)
            if contract:
                contracts.append(contract)
        return contracts

    @staticmethod
    def _resolve_ticks_query_type(query_type: str) -> Any:
        """Map a string query type to the SDK enum value."""
        if sj is None:
            return query_type
        ticks_qt = getattr(getattr(sj, "constant", None), "TicksQueryType", None)
        if ticks_qt is None:
            return query_type
        return getattr(ticks_qt, query_type, getattr(ticks_qt, "AllDay", query_type))
