"""Fubon real-time market data (quote) runtime."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("feed_adapter.fubon.quote_runtime")

_PRICE_SCALE: int = 10_000
_BOOK_LEVELS: int = 5


class FubonQuoteRuntime:
    """Manages Fubon real-time market data WebSocket subscriptions."""

    __slots__ = (
        "_sdk",
        "_on_tick",
        "_on_bidask",
        "_watchdog_thread",
        "_subscribed",
        "_running",
        "_tick_buffer",
        "_bidask_buffer",
        "_last_data_ts",
        "log",
    )

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self._on_tick: Callable[..., Any] | None = None
        self._on_bidask: Callable[..., Any] | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._subscribed: set[str] = set()
        self._running: bool = False
        self._last_data_ts: float = 0.0
        self.log = logger
        self._tick_buffer: dict[str, Any] = {
            "code": "",
            "close": 0,
            "volume": 0,
            "ts": 0,
        }
        self._bidask_buffer: dict[str, Any] = {
            "code": "",
            "bid_price": [0] * _BOOK_LEVELS,
            "bid_volume": [0] * _BOOK_LEVELS,
            "ask_price": [0] * _BOOK_LEVELS,
            "ask_volume": [0] * _BOOK_LEVELS,
            "ts": 0,
        }

    def register_quote_callbacks(
        self,
        on_tick: Callable[..., Any],
        on_bidask: Callable[..., Any],
    ) -> None:
        self._on_tick = on_tick
        self._on_bidask = on_bidask
        self.log.info("Fubon quote callbacks registered")

    def subscribe(self, symbols: list[str]) -> None:
        if not self._running:
            self._sdk.init_realtime()
            self._running = True

        ws = self._sdk.marketdata.websocket_client.stock
        for sym in symbols:
            if sym in self._subscribed:
                continue
            ws.subscribe(
                {"channel": "trades", "symbol": sym},
                on_message=self._on_fubon_trade,
            )
            ws.subscribe(
                {"channel": "books", "symbol": sym},
                on_message=self._on_fubon_book,
            )
            self._subscribed.add(sym)
            self.log.info("Fubon subscribed", symbol=sym)

    def unsubscribe(self, symbols: list[str]) -> None:
        ws = self._sdk.marketdata.websocket_client.stock
        for sym in symbols:
            if sym not in self._subscribed:
                continue
            try:
                ws.unsubscribe({"channel": "trades", "symbol": sym})
            except Exception as exc:
                self.log.warning("Fubon unsubscribe trades failed", symbol=sym, error=str(exc))
            try:
                ws.unsubscribe({"channel": "books", "symbol": sym})
            except Exception as exc:
                self.log.warning("Fubon unsubscribe books failed", symbol=sym, error=str(exc))
            self._subscribed.discard(sym)
            self.log.info("Fubon unsubscribed", symbol=sym)

    def _on_fubon_trade(self, data: Any) -> None:
        if self._on_tick is None:
            return
        try:
            buf = self._tick_buffer
            symbol = _get(data, "symbol", "")
            price_raw = _get(data, "close", None)
            if price_raw is None:
                price_raw = _get(data, "price", 0)
            volume = int(_get(data, "volume", 0))
            ts_raw = _get(data, "datetime", None)
            price_scaled = int(float(price_raw) * _PRICE_SCALE)
            ts_ns = _ts_to_ns(ts_raw)
            buf["code"] = symbol
            buf["close"] = price_scaled
            buf["volume"] = volume
            buf["ts"] = ts_ns
            self._last_data_ts = time.monotonic()
            self._on_tick(buf)
        except Exception as exc:
            self.log.error("Fubon trade callback error", error=str(exc))

    def _on_fubon_book(self, data: Any) -> None:
        if self._on_bidask is None:
            return
        try:
            buf = self._bidask_buffer
            symbol = _get(data, "symbol", "")
            ts_raw = _get(data, "datetime", None)
            ts_ns = _ts_to_ns(ts_raw)
            bid_prices_raw = _get(data, "bid_prices", [])
            bid_sizes_raw = _get(data, "bid_sizes", [])
            ask_prices_raw = _get(data, "ask_prices", [])
            ask_sizes_raw = _get(data, "ask_sizes", [])
            n_bp = len(bid_prices_raw)
            n_bv = len(bid_sizes_raw)
            n_ap = len(ask_prices_raw)
            n_av = len(ask_sizes_raw)
            bp = buf["bid_price"]
            bv = buf["bid_volume"]
            ap = buf["ask_price"]
            av = buf["ask_volume"]
            for i in range(_BOOK_LEVELS):
                bp[i] = int(float(bid_prices_raw[i]) * _PRICE_SCALE) if i < n_bp else 0
                bv[i] = int(bid_sizes_raw[i]) if i < n_bv else 0
                ap[i] = int(float(ask_prices_raw[i]) * _PRICE_SCALE) if i < n_ap else 0
                av[i] = int(ask_sizes_raw[i]) if i < n_av else 0
            buf["code"] = symbol
            buf["ts"] = ts_ns
            self._last_data_ts = time.monotonic()
            self._on_bidask(buf)
        except Exception as exc:
            self.log.error("Fubon book callback error", error=str(exc))

    def start_quote_watchdog(self, timeout_s: float = 30.0) -> None:
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._running = True
        self.log.info("Starting Fubon quote watchdog", timeout_s=timeout_s)

        def _watch() -> None:
            while self._running:
                time.sleep(timeout_s)
                if not self._running:
                    break
                last = self._last_data_ts
                if last <= 0:
                    continue
                gap = time.monotonic() - last
                if gap >= timeout_s:
                    self.log.warning(
                        "Fubon quote watchdog: no data",
                        gap_s=round(gap, 3),
                        timeout_s=timeout_s,
                    )

        self._watchdog_thread = threading.Thread(
            target=_watch,
            name="fubon-quote-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._subscribed:
            self.unsubscribe(list(self._subscribed))
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=5.0)
            self._watchdog_thread = None
        self.log.info("Fubon quote runtime stopped")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _ts_to_ns(ts_val: Any) -> int:
    if ts_val is None:
        return 0
    return timebase.coerce_ns(ts_val)
