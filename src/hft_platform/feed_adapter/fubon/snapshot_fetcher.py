"""Fubon REST snapshot fetcher for initial state bootstrapping."""

from __future__ import annotations

from typing import Any

import structlog

from hft_platform.core import timebase

logger = structlog.get_logger(__name__)

PRICE_SCALE: int = 10_000


def _scale_price(raw: Any) -> int:
    """Convert a raw price value to scaled int (x10000).

    Handles float, str, int, and None inputs gracefully.
    Returns 0 for None or unparseable values.
    """
    if raw is None:
        return 0
    try:
        return int(float(raw) * PRICE_SCALE)
    except (ValueError, TypeError):
        return 0


class FubonSnapshotFetcher:
    """Fetches current market snapshots via Fubon REST API.

    This is a cold-path operation used at startup to bootstrap initial
    market state before the streaming quote feed begins.
    """

    __slots__ = ("_sdk",)

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def fetch_snapshots(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Fetch intraday quote snapshots for the given symbols.

        Per-symbol error isolation: if one symbol fails, it is logged
        and skipped. Returns only successfully fetched snapshots.
        """
        if not symbols:
            return []

        results: list[dict[str, Any]] = []
        for sym in symbols:
            try:
                resp = self._sdk.marketdata.rest_client.stock.intraday.quote(
                    symbol=sym,
                )
                snapshot = self._translate(sym, resp)
                results.append(snapshot)
                logger.debug("fubon_snapshot_fetched", symbol=sym)
            except Exception:
                logger.warning("fubon_snapshot_fetch_failed", symbol=sym, exc_info=True)
        return results

    @staticmethod
    def _translate(symbol: str, raw: Any) -> dict[str, Any]:
        """Translate SDK response to canonical snapshot format."""
        # Support both dict and object attribute access.
        get = raw.get if isinstance(raw, dict) else (lambda key, default=None: getattr(raw, key, default))

        return {
            "code": symbol,
            "close": _scale_price(get("close")),
            "volume": int(get("volume", 0) or 0),
            "bid_price": _scale_price(get("bid_price")),
            "ask_price": _scale_price(get("ask_price")),
            "open": _scale_price(get("open")),
            "high": _scale_price(get("high")),
            "low": _scale_price(get("low")),
            "ts": timebase.now_ns(),
        }
