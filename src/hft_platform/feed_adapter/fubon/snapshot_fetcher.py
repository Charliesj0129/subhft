"""Fubon REST snapshot fetcher for initial state bootstrapping."""

from __future__ import annotations

from typing import Any

import structlog

from hft_platform.core import timebase

logger = structlog.get_logger(__name__)


def _coerce_price(raw: Any) -> float:
    """Coerce a raw price value to float for normalizer consumption.

    The normalizer applies per-symbol scaling (x10000 or as configured).
    This function only handles type coercion, NOT scaling.
    Returns 0.0 for None or unparseable values.
    """
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


def _coerce_int(raw: Any) -> int:
    """Coerce a raw size / volume to int; 0 for None or unparseable values."""
    if raw is None:
        return 0
    try:
        return int(raw)
    except (ValueError, TypeError, OverflowError):
        return 0


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """The nested object at *key* of a quote response, or an empty dict when absent / not an object."""
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


class FubonSnapshotFetcher:
    """Fetches current market snapshots via Fubon REST API.

    This is a cold-path operation used at startup to bootstrap initial
    market state before the streaming quote feed begins.

    Uses the futures/options REST client of fubon-neo 2.5.0,
    ``sdk.marketdata.rest_client.futopt.intraday.quote(symbol=...)``.
    ``sdk.marketdata`` only exists after ``sdk.init_realtime()``.
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
                resp = self._sdk.marketdata.rest_client.futopt.intraday.quote(
                    symbol=sym,
                )
                snapshot = self._translate(sym, resp)
                results.append(snapshot)
                logger.debug("fubon_snapshot_fetched", symbol=sym)
            except Exception as _exc:  # noqa: BLE001
                logger.warning("fubon_snapshot_fetch_failed", symbol=sym, exc_info=True)
        return results

    @staticmethod
    def _translate(symbol: str, raw: Any) -> dict[str, Any]:
        """Translate a documented ``intraday/quote`` response to the canonical snapshot format.

        Source fields: ``lastTrade.price`` / ``lastTrade.size`` (the last REAL trade;
        ``lastPrice`` / ``lastSize`` / ``change`` include trial matching and are not used),
        ``total.tradeVolume``, ``openPrice``, ``highPrice``, ``lowPrice``.  Prices stay raw
        numbers; the normalizer applies the per-symbol scale.

        The response carries no live best bid / ask and no sizes: ``lastTrade.bid`` /
        ``lastTrade.ask`` are the quotes at the last trade, possibly hours old, and
        feeding them to the normalizer would seed the book with a stale quote.  So
        ``buy_*`` / ``sell_*`` are 0, which ``normalize_snapshot`` reads as "no level".

        ``ts`` is the fetch time, not the response's ``lastUpdated``: a source time from
        the previous session would be counted as feed time skew on the snapshot topic.
        """
        if not isinstance(raw, dict):
            raise TypeError(f"intraday quote response must be an object, got {type(raw).__name__}")
        last = _section(raw, "lastTrade")
        total = _section(raw, "total")

        return {
            "code": symbol,
            "close": _coerce_price(last.get("price")),
            "volume": _coerce_int(last.get("size")),
            "total_volume": _coerce_int(total.get("tradeVolume")),
            "buy_price": 0.0,
            "sell_price": 0.0,
            "buy_volume": 0,
            "sell_volume": 0,
            "open": _coerce_price(raw.get("openPrice")),
            "high": _coerce_price(raw.get("highPrice")),
            "low": _coerce_price(raw.get("lowPrice")),
            "ts": timebase.now_ns(),
        }
