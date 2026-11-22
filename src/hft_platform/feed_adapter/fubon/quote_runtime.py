"""Fubon real-time market data (quote) runtime.

Manages WebSocket subscriptions via the ``fubon_neo`` SDK and translates
the documented futures/options ``trades`` and ``books`` payloads into the
platform's canonical format before forwarding to registered callbacks.

Wiring (fubon-neo 2.5.0, futures/options)
-----------------------------------------
``sdk.init_realtime(mode, version=...)`` creates ``sdk.marketdata``.  This runtime
then uses ``sdk.marketdata.websocket_client.futopt``: one ``on("message", handler)``
listener, one ``on("error", handler)`` listener and a single ``connect()``;
``subscribe(params)`` / ``unsubscribe(params)`` each take ONE dict.  The message
listener receives the raw JSON string of an ``event``/``data`` envelope.
``subscribed`` events carry the subscription id that ``unsubscribe`` needs.

Restart (generation counter)
----------------------------
``stop()`` releases the connection and bumps ``_generation``; the next ``subscribe()``
opens a fresh connection (new ``init_realtime``, new listeners) and clears ``_stopped``.
Every listener handed to the SDK is a closure bound to the generation current when its
connection was made, and drops frames once the generation has moved on.  The SDK keeps
listener references after ``stop()``, so without this a late frame of the old connection
would be published a second time next to the new connection's frames.  Dropped frames are
counted in the throttled "message dropped" warning (reasons ``stale_generation_message`` /
``stale_generation_error``), not in a metric.  The quote watchdog thread is bound the same way.

Design notes
------------
- **Allocator Law**: The tick translation buffer (_tick_buffer) is
  pre-allocated once in ``__init__`` and reused as *workspace* to avoid
  intermediate allocations during the field-translation step.  However, a
  fresh snapshot dict is created for every callback invocation so the
  callback (and any downstream async queue) receives an independent object
  that cannot be overwritten by the next WebSocket message.  Book frames carry
  0 to 5 levels, so their lists are built per frame and owned by the consumer.
- **Thread Safety**: Fubon WebSocket callbacks run in a separate thread; the
  consumer runs in an asyncio event loop.  The snapshot dict ensures that by
  the time the event loop dequeues the message the values are still correct —
  the workspace buffer may already have been overwritten by the next callback.
  This module never touches the event loop: it calls the registered callbacks
  on the SDK thread, and the registered callback (the market-data service's)
  is what enters the loop via ``call_soon_threadsafe``.
- **Precision Law**: Raw float prices are passed through to the normalizer,
  which applies per-symbol scaling (x10000 or as configured in symbols.yaml).
  Pre-scaling here would cause double-scaling since the normalizer always
  scales its input.
- **Boundary Law**: All Fubon-specific names are translated to canonical keys
  (``code``, ``close``, ``volume``, ``total_volume``, ``bid_price``, ``ask_price``, etc.) so
  the normalizer can process them without broker awareness.
- **Documented payloads**: ``trades`` ``data`` = ``symbol``, ``trades[0].price/size``
  (``bid``/``ask`` are not forwarded: no tick field carries them), ``total.tradeVolume``
  (-> ``total_volume``), ``time``, ``serial`` (not forwarded).  ``books`` ``data`` =
  ``symbol``, ``bids``/``asks`` as lists of ``{price, size}`` with 0 to 5 levels
  (only the levels present are forwarded, never zero-filled), ``time``;
  ``derivedBid``/``derivedAsk`` are ignored.
  ``time`` is epoch MICROSECONDS (the pages do not state the unit; inferred from their
  examples) and becomes epoch nanoseconds.  A payload missing a required field, or
  with a ``time`` outside the microsecond range, is dropped and counted in the
  throttled "message dropped" warning, never forwarded.
- **Trial frames**: fubon-neo 2.5.0 streams futopt v1.1 by default, which adds
  trial-matching frames (``isTrial: true``) on the trades/books channels.  A
  trial price is not a trade and not a tradable book, so those frames are
  dropped (and counted) before any translation reaches the normalizer.  A
  trial frame still proves the feed is alive, so it refreshes the watchdog
  timestamp.  ``isTrial`` is checked on the envelope and on the payload.
- **Realtime mode**: ``HFT_FUBON_REALTIME_MODE`` (``speed`` | ``normal``,
  default ``speed``) is read once in ``__init__``; an invalid value raises
  ``ValueError`` there so a misconfigured process refuses to start.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any, Callable

import orjson
from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter.fubon.quote_runtime")

# Maximum order book levels forwarded per side (the service sends 0 to 5).
_BOOK_LEVELS: int = 5

# Frame time is epoch microseconds (inferred from the documented examples).
# Values outside [_EPOCH_US_MIN, _EPOCH_US_MAX) (1973 .. year 5138 as microseconds)
# are not treated as microseconds and the frame is dropped.
_NS_PER_US: int = 1_000
_EPOCH_US_MIN: int = 10**14
_EPOCH_US_MAX: int = 10**17

# Realtime (market data WebSocket) mode, read once at construction.
_REALTIME_MODE_ENV: str = "HFT_FUBON_REALTIME_MODE"
_MODE_SPEED: str = "speed"
_MODE_NORMAL: str = "normal"
_REALTIME_MODES: tuple[str, ...] = (_MODE_SPEED, _MODE_NORMAL)

# futopt streaming version requested from the SDK.  v1.1 is what fubon-neo 2.5.0
# serves when nothing is passed; it is spelled out so an SDK default change
# cannot silently change which frames reach this runtime.
_FUTOPT_STREAM_VERSION: str = "v1.1"

# Top-level key carried by futopt v1.1 trial-matching frames on trades/books.
_TRIAL_FLAG: str = "isTrial"
_CHANNEL_TRADES: str = "trades"
_CHANNEL_BOOKS: str = "books"
_CHANNELS: tuple[str, ...] = (_CHANNEL_TRADES, _CHANNEL_BOOKS)

# WebSocket envelope ``event`` values this runtime acts on.  Others
# (authenticated, heartbeat, pong, subscriptions) need no action.
_EVENT_DATA: str = "data"
_EVENT_SUBSCRIBED: str = "subscribed"
_EVENT_UNSUBSCRIBED: str = "unsubscribed"
_EVENT_ERROR: str = "error"

# Error-event codes documented for the futures/options market-data service.
_ERROR_CODE_REASONS: dict[int, str] = {
    1001: "subscription_limit_exceeded",
    1003: "connection_limit_reached",
}

# A message that cannot be handled is counted per reason; the warning is logged
# on the first occurrence and then every this-many, so a bad stream cannot flood the log.
_UNHANDLED_LOG_EVERY: int = 1000
_MAX_LOGGED_MESSAGE_CHARS: int = 200


class FubonQuoteRuntime:
    """Manages Fubon real-time market data WebSocket subscriptions."""

    __slots__ = (
        "_sdk",
        "_on_tick",
        "_on_bidask",
        "_watchdog_thread",
        "_subscribed",
        "_running",
        "_stopped",
        "_generation",
        "_tick_buffer",
        "_last_data_ts",
        "_realtime_mode",
        "_trial_drop_counters",
        "_ws",
        "_sub_ids",
        "_id_key",
        "_unhandled_counts",
        "log",
    )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self._on_tick: Callable[..., Any] | None = None
        self._on_bidask: Callable[..., Any] | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._subscribed: set[str] = set()
        self._running: bool = False
        # Set True once ``stop()`` has been invoked and cleared again by the next
        # successful connect (restart).  The Fubon SDK retains references to the
        # listeners after stop, so late-arriving callbacks could still double-publish
        # into the canonical pipeline during a resubscribe (P1, 2026-04-24). The
        # callbacks check this flag first and silently drop once stopped.
        self._stopped: bool = False
        # Bumped by stop().  Each connection's listeners are closures over the value
        # current when they were registered (see _message_listener), so frames from a
        # retired connection are dropped even after a restart has cleared _stopped.
        self._generation: int = 0
        self._last_data_ts: float = 0.0
        self.log = logger
        # Validated here, not at subscribe time, so a bad value stops startup
        # before any broker session work happens.
        self._realtime_mode: str = _read_realtime_mode()
        # Labelled children of fubon_trial_frames_dropped_total, resolved on
        # the first drop of each channel and reused after (the registry is not
        # touched at construction and normal frames never reach this dict).
        self._trial_drop_counters: dict[str, Any] = {}
        # futopt WebSocket client; set once init_realtime + on() + connect() succeeded.
        self._ws: Any = None
        # (channel, symbol) -> subscription id, filled from ``subscribed`` events
        # (WebSocket thread) and read by unsubscribe(); id -> (channel, symbol) lets a
        # data frame without a ``channel`` key still be routed and lets an
        # ``unsubscribed`` event that carries only the id be forgotten.  Both are
        # bounded by the service's 300-subscription cap.
        self._sub_ids: dict[tuple[str, str], str] = {}
        self._id_key: dict[str, tuple[str, str]] = {}
        self._unhandled_counts: dict[str, int] = {}

        # Pre-allocated translation buffer used as *workspace* (Allocator Law).
        # Values are overwritten on each callback to perform field translation
        # without intermediate allocations.  A fresh snapshot dict is then
        # passed to the registered callback — see _on_fubon_trade for details.
        self._tick_buffer: dict[str, Any] = {
            "code": "",
            "close": 0,
            "volume": 0,
            "total_volume": 0,
            "ts": 0,
        }

    # ------------------------------------------------------------------ #
    # Callback registration
    # ------------------------------------------------------------------ #

    def register_quote_callbacks(
        self,
        on_tick: Callable[..., Any],
        on_bidask: Callable[..., Any],
    ) -> None:
        """Register canonical tick and bidask callbacks."""
        self._on_tick = on_tick
        self._on_bidask = on_bidask
        self.log.info("Fubon quote callbacks registered")

    # ------------------------------------------------------------------ #
    # Subscription management
    # ------------------------------------------------------------------ #

    def subscribe(self, symbols: list[str], *, after_hours: bool = False) -> None:
        """Subscribe to trades and books channels for the given symbols.

        On first call: ``sdk.init_realtime(mode, version={"futopt": "v1.1"})``,
        then register the message and error listeners on
        ``sdk.marketdata.websocket_client.futopt`` and ``connect()`` once.  ``mode``
        comes from ``HFT_FUBON_REALTIME_MODE`` (default ``speed``).  If the SDK
        ``Mode`` enum cannot be imported, or ``connect()`` raises, the exception
        propagates and the runtime stays uninitialised (startup refused).

        Each symbol then gets one ``subscribe({"channel": ..., "symbol": ...})`` per
        channel (``trades`` = tick, ``books`` = L5 bid/ask).  ``after_hours=True``
        adds ``"afterHours": True`` to select the night session (documented for
        futures/options market data); the key is omitted for the day session.  A
        symbol already subscribed is skipped whatever its session.

        After ``stop()`` this reconnects: the runtime is restartable, and frames of the
        stopped connection stay dropped (see "Restart" in the module docstring).
        """
        if self._ws is None:
            self._connect()

        ws = self._ws
        for sym in symbols:
            if sym in self._subscribed:
                continue
            for channel in _CHANNELS:
                params: dict[str, Any] = {"channel": channel, "symbol": sym}
                if after_hours:
                    params["afterHours"] = True
                ws.subscribe(params)
            self._subscribed.add(sym)
            self.log.info("Fubon subscribed", symbol=sym, after_hours=after_hours)

    def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe from trades and books channels for the given symbols.

        The service unsubscribes by subscription id (``{"id": ...}``), which it
        reports in the ``subscribed`` event.  A channel whose id has not been seen
        cannot be unsubscribed: that is logged and skipped.
        """
        ws = self._ws
        for sym in symbols:
            if sym not in self._subscribed:
                continue
            for channel in _CHANNELS:
                sub_id = self._sub_ids.pop((channel, sym), None)
                if sub_id is None or ws is None:
                    self.log.warning(
                        "Fubon unsubscribe skipped: no subscription id",
                        symbol=sym,
                        channel=channel,
                    )
                    continue
                self._id_key.pop(sub_id, None)
                try:
                    ws.unsubscribe({"id": sub_id})
                except Exception as exc:
                    self.log.warning(
                        "Fubon unsubscribe failed",
                        symbol=sym,
                        channel=channel,
                        error=str(exc),
                    )
            self._subscribed.discard(sym)
            self.log.info("Fubon unsubscribed", symbol=sym)

    def _connect(self) -> None:
        """Create the realtime session and connect the futopt WebSocket once.

        Also the restart path: after ``stop()`` the next ``subscribe()`` lands here.  The
        listeners are bound to the generation current now, and ``_stopped`` is cleared only
        once the connection is up, so a failed connect leaves a stopped runtime stopped.  A failed
        connect also bumps the generation: its listeners may stay attached to a reused client.
        """
        generation = self._generation
        mode = _resolve_realtime_mode(self._realtime_mode)
        self._sdk.init_realtime(mode, version={"futopt": _FUTOPT_STREAM_VERSION})
        ws = self._sdk.marketdata.websocket_client.futopt
        try:
            # The SDK keeps these listeners after stop(); they drop frames once the generation moves on.
            ws.on("message", self._message_listener(generation))
            # An "error" event with no listener is raised inside the SDK's own thread.
            ws.on("error", self._error_listener(generation))
            ws.connect()
        except Exception:
            # A failed attempt may leave its listeners on a client object the SDK reuses; retire
            # them so the retry's listeners are the only live ones.
            self._generation += 1
            raise
        self._ws = ws
        self._stopped = False
        self._running = True
        self.log.info(
            "Fubon realtime initialised",
            mode=self._realtime_mode,
            futopt_version=_FUTOPT_STREAM_VERSION,
        )

    # ------------------------------------------------------------------ #
    # WebSocket message handling — raw JSON envelope → channel handlers
    # ------------------------------------------------------------------ #

    def _message_listener(self, generation: int) -> Callable[[Any], None]:
        """The ``message`` listener of the connection made in *generation*.

        Built once per connection (not per frame).  After ``stop()`` has bumped
        ``_generation`` it drops everything, including after a restart has cleared ``_stopped``.
        """

        def listener(raw: Any) -> None:
            if generation != self._generation:
                self._note_unhandled("stale_generation_message")
                return
            self._on_ws_message(raw)

        return listener

    def _error_listener(self, generation: int) -> Callable[[Any], None]:
        """The ``error`` listener of the connection made in *generation* (stale errors are counted, not logged)."""

        def listener(error: Any) -> None:
            if generation != self._generation:
                self._note_unhandled("stale_generation_error")
                return
            self._on_ws_error(error)

        return listener

    def _on_ws_message(self, raw: Any) -> None:
        """Parse one raw WebSocket message and act on its ``event``."""
        if self._stopped:
            return
        try:
            msg = _parse_message(raw)
            if msg is None:
                self._note_unhandled("message_not_json_object")
                return
            event = msg.get("event")
            if event == _EVENT_DATA:
                self._route_data(msg)
            elif event == _EVENT_SUBSCRIBED:
                self._track_subscribed(msg.get("data"))
            elif event == _EVENT_UNSUBSCRIBED:
                self._track_unsubscribed(msg.get("data"))
            elif event == _EVENT_ERROR:
                self._handle_error_event(msg)
        except orjson.JSONDecodeError:
            self._note_unhandled("message_malformed_json")
        except Exception as exc:
            self.log.error("Fubon websocket message handler error", error=str(exc))

    def _on_ws_error(self, error: Any) -> None:
        """Log a transport-level error raised by the SDK's WebSocket client."""
        self.log.error("Fubon websocket transport error", error=str(error))

    def _route_data(self, msg: dict[str, Any]) -> None:
        """Send a ``data`` event to the trades or books handler.

        The channel comes from the envelope, else from the subscription id the
        ``subscribed`` event registered.  An envelope-level ``isTrial`` is dropped
        here; a payload-level one is dropped by the channel handler.
        """
        payload = msg.get("data")
        if not isinstance(payload, dict):
            self._note_unhandled("data_payload_not_object")
            return
        channel = msg.get("channel")
        if not isinstance(channel, str):
            sub_id = msg.get("id")
            key = self._id_key.get(sub_id) if isinstance(sub_id, str) else None
            channel = key[0] if key is not None else None
        if channel == _CHANNEL_TRADES:
            if _is_trial(msg):
                self._drop_trial(_CHANNEL_TRADES)
            else:
                self._on_fubon_trade(payload)
        elif channel == _CHANNEL_BOOKS:
            if _is_trial(msg):
                self._drop_trial(_CHANNEL_BOOKS)
            else:
                self._on_fubon_book(payload)
        else:
            self._note_unhandled("data_channel_unknown")

    def _track_subscribed(self, data: Any) -> None:
        """Remember the subscription id of each acknowledged (channel, symbol)."""
        for entry in _entries(data):
            sub_id = entry.get("id")
            channel = entry.get("channel")
            symbol = entry.get("symbol")
            if not (isinstance(sub_id, str) and sub_id and isinstance(channel, str) and isinstance(symbol, str)):
                self._note_unhandled("subscribed_event_malformed")
                continue
            stale_id = self._sub_ids.get((channel, symbol))
            if stale_id is not None and stale_id != sub_id:
                self._id_key.pop(stale_id, None)
            self._sub_ids[(channel, symbol)] = sub_id
            self._id_key[sub_id] = (channel, symbol)
            self.log.info("Fubon subscription acknowledged", channel=channel, symbol=symbol)

    def _track_unsubscribed(self, data: Any) -> None:
        """Forget ids the service reports as unsubscribed."""
        for entry in _entries(data):
            sub_id = entry.get("id")
            if not isinstance(sub_id, str):
                continue
            key = self._id_key.pop(sub_id, None)
            if key is not None and self._sub_ids.get(key) == sub_id:
                del self._sub_ids[key]

    def _handle_error_event(self, msg: dict[str, Any]) -> None:
        """Log an ``error`` event; 1001 = subscription limit, 1003 = connection limit."""
        code = msg.get("code")
        payload = msg.get("data")
        text = payload.get("message") if isinstance(payload, dict) else None
        reason = _ERROR_CODE_REASONS.get(code, "error") if isinstance(code, int) else "error"
        self.log.error(
            "Fubon websocket error event",
            code=code,
            reason=reason,
            message=str(text)[:_MAX_LOGGED_MESSAGE_CHARS] if text is not None else None,
        )

    def _drop_trial(self, channel: str) -> None:
        """Drop a trial-matching frame: it proves liveness but is never forwarded."""
        self._last_data_ts = time.monotonic()
        self._count_trial_drop(channel)

    def _note_unhandled(self, reason: str) -> None:
        """Count a message that could not be handled; log the first and every Nth."""
        count = self._unhandled_counts.get(reason, 0) + 1
        self._unhandled_counts[reason] = count
        if count == 1 or count % _UNHANDLED_LOG_EVERY == 0:
            self.log.warning("Fubon websocket message dropped", reason=reason, count=count)

    # ------------------------------------------------------------------ #
    # Internal callbacks — translate Fubon → canonical
    # ------------------------------------------------------------------ #

    def _on_fubon_trade(self, data: Any) -> None:
        """Translate a documented futopt ``trades`` payload to a canonical tick dict and forward.

        *data* is the ``data`` object of a ``trades`` event: ``symbol``, ``trades[0]``
        (``price``, ``size``, ``bid``, ``ask``), ``total.tradeVolume`` and ``time``
        (epoch microseconds).  A malformed payload is dropped and counted, never forwarded.
        """
        # P1 (2026-04-24): the SDK keeps a bound reference to this method
        # after stop(); late deliveries must not leak into the pipeline.
        if self._stopped or self._on_tick is None:
            return
        try:
            if not isinstance(data, dict):
                self._note_unhandled("trade_payload_not_object")
                return
            # futopt v1.1 trial-matching frame: not a trade.  Dropped before any
            # translation so it can never reach the normalizer callback.
            if _is_trial(data):
                self._drop_trial(_CHANNEL_TRADES)
                return

            fields = _parse_trade(data)
            if isinstance(fields, str):
                self._note_unhandled(fields)
                return
            symbol, price, size, total_volume, ts_ns = fields
            if len(data["trades"]) > 1:
                # Only trades[0] is forwarded (one tick per frame); say so rather than lose ticks silently.
                self._note_unhandled("trade_extra_entries_ignored")

            buf = self._tick_buffer
            # Pass the raw price — the normalizer applies per-symbol scaling.
            # Pre-scaling here would cause double-scaling (DATA-001).
            buf["code"] = symbol
            buf["close"] = price
            buf["volume"] = size
            buf["total_volume"] = total_volume
            buf["ts"] = ts_ns

            self._last_data_ts = time.monotonic()
            # Create a fresh snapshot dict so the async consumer receives an
            # independent object.  The workspace buf may be overwritten by the
            # next callback before the event loop dequeues this message.
            self._on_tick(
                {
                    "code": buf["code"],
                    "close": buf["close"],
                    "volume": buf["volume"],
                    "total_volume": buf["total_volume"],
                    "ts": buf["ts"],
                }
            )
        except Exception as exc:
            self.log.error("Fubon trade callback error", error=str(exc))

    def _on_fubon_book(self, data: Any) -> None:
        """Translate a documented futopt ``books`` payload to a canonical bidask dict and forward.

        *data* is the ``data`` object of a ``books`` event: ``symbol``, ``bids`` / ``asks``
        (lists of ``{price, size}``, best first, 0 to 5 levels) and ``time`` (epoch
        microseconds).  ``derivedBid`` / ``derivedAsk`` are ignored.  Only the levels the book
        has are forwarded (an empty side is an empty list); nothing is zero-filled.
        """
        # P1 (2026-04-24): the SDK keeps a bound reference to this method
        # after stop(); late deliveries must not leak into the pipeline.
        if self._stopped or self._on_bidask is None:
            return
        try:
            if not isinstance(data, dict):
                self._note_unhandled("book_payload_not_object")
                return
            # futopt v1.1 trial-matching frame: bids/asks are the leftover book of a
            # trial phase, not a tradable one.  Dropped before any translation so it
            # can never reach the normalizer callback.
            if _is_trial(data):
                self._drop_trial(_CHANNEL_BOOKS)
                return

            fields = _parse_book(data)
            if isinstance(fields, str):
                self._note_unhandled(fields)
                return
            symbol, (bid_prices, bid_sizes), (ask_prices, ask_sizes), ts_ns = fields

            self._last_data_ts = time.monotonic()
            # The lists are built per frame because the depth varies (0 to 5 levels),
            # so the consumer owns them outright and no later frame can overwrite them.
            self._on_bidask(
                {
                    "code": symbol,
                    "bid_price": bid_prices,
                    "bid_volume": bid_sizes,
                    "ask_price": ask_prices,
                    "ask_volume": ask_sizes,
                    "ts": ts_ns,
                }
            )
        except Exception as exc:
            self.log.error("Fubon book callback error", error=str(exc))

    def _count_trial_drop(self, channel: str) -> None:
        """Count one dropped trial frame in ``fubon_trial_frames_dropped_total``."""
        counter = self._trial_drop_counters.get(channel)
        if counter is None:
            counter = MetricsRegistry.get().fubon_trial_frames_dropped_total.labels(channel=channel)
            self._trial_drop_counters[channel] = counter
        counter.inc()

    # ------------------------------------------------------------------ #
    # Watchdog
    # ------------------------------------------------------------------ #

    def start_quote_watchdog(self, timeout_s: float = 30.0) -> None:
        """Start a watchdog thread that monitors data freshness.

        If no data arrives for *timeout_s* seconds, a warning is logged.
        """
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._running = True
        generation = self._generation
        self.log.info("Starting Fubon quote watchdog", timeout_s=timeout_s)

        def _watch() -> None:
            # Bound to the generation it was started in: stop() may not outlast a sleeping
            # thread (its join times out), and a restart sets _running again, so without the
            # generation check the old thread would keep running next to the new one.
            while self._running:
                time.sleep(timeout_s)
                if not self._running or self._generation != generation:
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

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        """Stop the watchdog and unsubscribe all symbols.

        Sets ``_stopped`` and bumps ``_generation`` before unsubscribing so that any
        callbacks still queued by the Fubon SDK drop their payloads instead of emitting
        them into the canonical pipeline (P1, 2026-04-24).  The bump is what keeps them
        dropped after a restart: the next ``subscribe()`` clears ``_stopped`` again, but
        the old connection's listeners stay bound to the old generation.
        """
        self._stopped = True
        self._generation += 1
        self._running = False
        if self._subscribed:
            self.unsubscribe(list(self._subscribed))
        # Release the connection: the service caps concurrent WebSocket connections
        # at 7, and the SDK's socket thread would otherwise outlive this runtime
        # (FubonClient.resubscribe builds a new runtime and connects again).
        # Ids belong to the connection being closed.
        self._sub_ids.clear()
        self._id_key.clear()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.disconnect()
            except Exception as exc:
                self.log.warning("Fubon websocket disconnect failed", error=str(exc))
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=5.0)
            self._watchdog_thread = None
        self.log.info("Fubon quote runtime stopped")


# ---------------------------------------------------------------------- #
# Module-level helpers (stateless, no allocation)
# ---------------------------------------------------------------------- #


def _read_realtime_mode() -> str:
    """Read ``HFT_FUBON_REALTIME_MODE`` and return ``"speed"`` or ``"normal"``.

    Unset or blank means the default (``speed``).  Matching ignores case and
    surrounding whitespace.  Any other value raises ``ValueError`` so a
    misconfigured process fails at startup instead of guessing a mode.
    """
    value = os.environ.get(_REALTIME_MODE_ENV, "").strip().lower()
    if not value:
        return _MODE_SPEED
    if value not in _REALTIME_MODES:
        raise ValueError(f"{_REALTIME_MODE_ENV} must be one of {'|'.join(_REALTIME_MODES)}, got {value!r}")
    return value


def _resolve_realtime_mode(name: str) -> Any:
    """Map a validated mode name to the SDK ``Mode`` enum member.

    The SDK is imported here, not at module load, so this module still imports
    without ``fubon_neo``.  An ImportError is deliberately not caught.
    """
    from fubon_neo.sdk import Mode

    return Mode.Speed if name == _MODE_SPEED else Mode.Normal


def _parse_message(raw: Any) -> dict[str, Any] | None:
    """Parse one raw WebSocket message into its envelope dict.

    The SDK hands the listener the raw JSON text.  Returns ``None`` when *raw* is
    not text/bytes or the JSON is not an object; invalid JSON raises
    ``orjson.JSONDecodeError``.
    """
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    msg = orjson.loads(raw)
    return msg if isinstance(msg, dict) else None


def _entries(data: Any) -> list[dict[str, Any]]:
    """Subscribe/unsubscribe events carry one dict, or a list of dicts for several symbols."""
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    return []


def _number(raw: Any) -> int | float | None:
    """A finite JSON number, or None.  ``bool`` is not a number here."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    return raw


def _us_to_ns(raw: Any) -> int | None:
    """Convert a futopt frame ``time`` (epoch microseconds) to epoch nanoseconds.

    The unit is not stated on the trades / books / getting-started pages; it is
    inferred from their examples, which only make sense as epoch microseconds
    (1702956487023000 = 2023-12-19 11:28:07.023 Asia/Taipei, inside the day session).
    A value outside the microsecond range (a different unit, zero, negative) is
    rejected with ``None`` instead of being scaled into a wrong time.
    """
    value = _number(raw)
    if value is None or not _EPOCH_US_MIN <= value < _EPOCH_US_MAX:
        return None
    return int(value) * _NS_PER_US


def _is_trial(data: dict[str, Any]) -> bool:
    """True when *data* is a futopt v1.1 trial-matching frame (``isTrial: true``).

    The service sends a JSON boolean.  ``1`` and ``"true"`` are accepted too so a
    representation change cannot let a trial price through as a trade.  An
    absent flag, ``None``, ``False`` or any other type is a normal frame.
    """
    flag = data.get(_TRIAL_FLAG)
    if isinstance(flag, str):
        return flag.strip().lower() == "true"
    if isinstance(flag, int):  # bool is an int subclass
        return flag == 1
    return False


def _total_volume(data: dict[str, Any]) -> int:
    """``total.tradeVolume`` of a trades payload; 0 when absent (as the normalizer defaults it)."""
    total = data.get("total")
    volume = _number(total.get("tradeVolume")) if isinstance(total, dict) else None
    return int(volume) if volume is not None else 0


def _parse_trade(data: dict[str, Any]) -> tuple[str, int | float, int, int, int] | str:
    """Documented trades payload -> ``(symbol, price, size, total_volume, ts_ns)``, or a reject reason."""
    symbol = data.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return "trade_symbol_missing"
    trades = data.get("trades")
    first = trades[0] if isinstance(trades, list) and trades else None
    if not isinstance(first, dict):
        return "trade_entry_missing"
    price = _number(first.get("price"))
    size = _number(first.get("size"))
    if price is None or size is None:
        return "trade_price_or_size_invalid"
    ts_ns = _us_to_ns(data.get("time"))
    if ts_ns is None:
        return "trade_time_invalid"
    return symbol, price, int(size), _total_volume(data), ts_ns


_Levels = tuple[list[int | float], list[int]]


def _parse_levels(raw: Any) -> _Levels | None:
    """Documented ``bids`` / ``asks`` -> ``(prices, sizes)``, best first, at most _BOOK_LEVELS; None if malformed.

    An absent side is empty (the field is optional); levels beyond _BOOK_LEVELS are ignored.
    """
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return None
    prices: list[int | float] = []
    sizes: list[int] = []
    for entry in raw[:_BOOK_LEVELS]:
        if not isinstance(entry, dict):
            return None
        price = _number(entry.get("price"))
        size = _number(entry.get("size"))
        if price is None or size is None:
            return None
        prices.append(price)
        sizes.append(int(size))
    return prices, sizes


def _parse_book(data: dict[str, Any]) -> tuple[str, _Levels, _Levels, int] | str:
    """Documented books payload -> ``(symbol, bids, asks, ts_ns)``, or a reject reason."""
    symbol = data.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return "book_symbol_missing"
    bids = _parse_levels(data.get("bids"))
    asks = _parse_levels(data.get("asks"))
    if bids is None or asks is None:
        return "book_levels_malformed"
    ts_ns = _us_to_ns(data.get("time"))
    if ts_ns is None:
        return "book_time_invalid"
    return symbol, bids, asks, ts_ns


def _coerce_price(raw: Any) -> float:
    """Coerce a raw Fubon quote price to float for normalizer consumption.

    The normalizer applies per-symbol scaling (x10000 or as configured).
    This function only handles type coercion, NOT scaling.
    """
    if raw is None:
        return 0.0
    return float(raw)
