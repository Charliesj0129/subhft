from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.scanner_gateway")

try:
    import shioaji as sj
except Exception:
    sj = None

_VALID_SCANNER_TYPES: frozenset[str] = frozenset(
    {
        "ChangePercentRank",
        "ChangePriceRank",
        "DayRangeRank",
        "VolumeRank",
        "AmountRank",
    }
)

_SCANNER_CACHE_TTL_S: float = 60.0


class ScannerGateway:
    """Market scanner queries for dynamic universe selection."""

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    def scan(
        self,
        scanner_type: str,
        ascending: bool = False,
        count: int = 50,
        timeout: int = 30000,
    ) -> Any:
        """Run a single scanner query.

        Parameters
        ----------
        scanner_type:
            One of ChangePercentRank, ChangePriceRank, DayRangeRank,
            VolumeRank, AmountRank.
        ascending:
            Sort direction.
        count:
            Maximum number of results.
        timeout:
            API timeout in milliseconds.
        """
        if scanner_type not in _VALID_SCANNER_TYPES:
            logger.warning("Invalid scanner type", scanner_type=scanner_type)
            raise ValueError(f"Invalid scanner_type '{scanner_type}'. Valid types: {sorted(_VALID_SCANNER_TYPES)}")

        if self._client.mode == "simulation":
            logger.info("Simulation mode: returning empty scanner result", scanner_type=scanner_type)
            return []

        cache_key = f"scanner:{scanner_type}:{ascending}:{count}"
        cached = self._client._cache_get(cache_key)
        if cached is not None:
            return cached

        if not self._client._rate_limit_api("scanners"):
            return []

        if sj is None:
            logger.warning("shioaji not available")
            return []

        type_enum = getattr(sj.constant.ScannerType, scanner_type, None)
        if type_enum is None:
            logger.warning("Scanner type not found in SDK enum", scanner_type=scanner_type)
            return []

        start_ns = time.perf_counter_ns()
        try:
            result = self._client.api.scanners(
                scanner_type=type_enum,
                ascending=ascending,
                count=count,
                timeout=timeout,
            )
            self._client._record_api_latency("scanners", start_ns, ok=True)
            self._client._cache_set(cache_key, _SCANNER_CACHE_TTL_S, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("scanners", start_ns, ok=False)
            logger.warning("Scanner query failed", scanner_type=scanner_type, error=str(exc))
            return []

    def scan_multiple(
        self,
        scanner_types: list[str] | None = None,
        count: int = 50,
        timeout: int = 30000,
    ) -> dict[str, Any]:
        """Run multiple scanner queries and return results keyed by type.

        Parameters
        ----------
        scanner_types:
            List of scanner type strings. Defaults to all 5 types.
        count:
            Maximum number of results per scanner.
        timeout:
            API timeout in milliseconds per scanner call.
        """
        types = list(scanner_types) if scanner_types is not None else sorted(_VALID_SCANNER_TYPES)
        results: dict[str, Any] = {}
        for st in types:
            try:
                results[st] = self.scan(st, count=count, timeout=timeout)
            except Exception as exc:
                logger.warning("scan_multiple: error for type", scanner_type=st, error=str(exc))
                results[st] = []
        return results
