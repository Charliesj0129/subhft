from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.scanner_gateway")

_VALID_SCANNER_TYPES = frozenset({
    "ChangePercentRank",
    "ChangePriceRank",
    "DayRangeRank",
    "VolumeRank",
    "AmountRank",
})


class ScannerGateway:
    """Dedicated market scanner gateway wrapping Shioaji api.scanners()."""

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

    def scan(
        self,
        scanner_type: str,
        ascending: bool = False,
        count: int = 100,
        date: str | None = None,
        timeout: int = 30000,
    ) -> list[Any]:
        """Run a market scanner. Wraps api.scanners().

        scanner_type: One of "ChangePercentRank", "ChangePriceRank",
                      "DayRangeRank", "VolumeRank", "AmountRank"
        ascending: Sort direction.
        count: Number of results to return.
        date: Date string (YYYY-MM-DD). None means today.
        timeout: Timeout in milliseconds.

        Returns a list of scanner results, or an empty list on error/simulation.
        """
        if scanner_type not in _VALID_SCANNER_TYPES:
            logger.warning(
                "Invalid scanner_type",
                scanner_type=scanner_type,
                valid=sorted(_VALID_SCANNER_TYPES),
            )
            return []

        if self._client.mode == "simulation":
            logger.info("Simulation mode: skipping scanner query")
            return []

        if not self._client.api or not self._client.logged_in:
            logger.warning("API not available for scanner query")
            return []

        if not hasattr(self._client.api, "scanners"):
            logger.warning("Shioaji API missing scanners method")
            return []

        sdk = self._sdk()
        if sdk is None:
            logger.warning("Shioaji SDK unavailable for scanner constant lookup")
            return []

        scanner_type_enum = self._resolve_scanner_type(sdk, scanner_type)
        if scanner_type_enum is None:
            logger.warning(
                "Could not resolve scanner type constant",
                scanner_type=scanner_type,
            )
            return []

        if not self._client._rate_limit_api("scanners"):
            logger.debug("Rate limited: scanners")
            return []

        start_ns = time.perf_counter_ns()
        try:
            result = self._client.api.scanners(
                scanner_type=scanner_type_enum,
                ascending=ascending,
                count=count,
                date=date,
                timeout=timeout,
            )
            self._client._record_api_latency("scanners", start_ns, ok=True)
            logger.info(
                "Scanner query completed",
                scanner_type=scanner_type,
                result_count=len(result) if result else 0,
            )
            return result if result is not None else []
        except Exception as exc:
            self._client._record_api_latency("scanners", start_ns, ok=False)
            logger.warning(
                "Scanner query failed",
                scanner_type=scanner_type,
                error=str(exc),
            )
            return []

    @staticmethod
    def _resolve_scanner_type(sdk: Any, scanner_type: str) -> Any | None:
        """Map a scanner_type string to the SDK ScannerType enum constant."""
        scanner_type_cls = getattr(
            getattr(sdk, "constant", None), "ScannerType", None
        )
        if scanner_type_cls is None:
            return None
        return getattr(scanner_type_cls, scanner_type, None)
