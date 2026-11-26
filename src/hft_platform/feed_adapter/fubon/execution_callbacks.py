"""Fubon execution callback adapter.

Translates Fubon SDK (fubon_neo 2.5.0) futures/options order, order-change,
fill and connection reporting into the platform's canonical callback interface
so downstream consumers (ExecutionNormalizer, PositionTracker, etc.) operate
without broker awareness.

SDK surface (introspected from the 2.5.0 wheel; each setter takes one callback)::

    sdk.set_on_futopt_order(cb)          order accepted / rejected
    sdk.set_on_order_futopt_changed(cb)  price / lot change, cancel
    sdk.set_on_futopt_filled(cb)         fill (FutOptFilledData)
    sdk.set_on_event(cb)                 connection / session events

The vendor convention is ``cb(code, content)``; the handlers below take
``*args`` and treat the last argument as the content so a one-argument
delivery is not lost.  The callbacks run on SDK threads.  The platform's
``_on_exec`` consumer hops onto the event loop itself (``call_soon_threadsafe``),
so this adapter forwards from the SDK thread and never touches the loop.

Design notes
------------
- **Fail closed**: ``register`` raises when the SDK lacks any required setter,
  before wiring any of them.  An order session whose fills go nowhere must not
  start (the old ``hasattr`` skip left exactly that behind silently).
- **Allocator Law**: Translation buffers (``_order_buffer``, ``_deal_buffer``)
  are pre-allocated once in ``__init__`` and reused by overwriting values in
  each callback invocation.  A lock keeps the SDK's callback threads from
  interleaving writes into one buffer.
- **Precision Law**: Raw prices from Fubon are passed through unscaled.
  ``ExecutionNormalizer`` is the single scaling point (via ``PriceCodec``).
  Pre-scaling here would cause double-scaling (x10000 * x10000 = x10^8).
- **Boundary Law**: All Fubon-specific field names are translated to canonical
  keys (``order_id``, ``symbol``, ``side``, ``price``, ``qty``, etc.) at this
  boundary.
- **Trading symbol**: SDK reports carry the *account / product* code (``FITX``) plus
  ``expiry_date`` (and ``strike_price`` / ``call_put`` for options), while the
  platform trades and keys positions by the trading symbol (``TXFL6``).
  ``FubonSymbolResolver`` converts with ``sdk.futopt.convert_symbol`` and caches the
  answer in a bounded dict.  A report whose symbol cannot be converted is NOT
  forwarded: a fill under the wrong symbol would open a phantom position under the
  product code and leave the real one flat.  It is logged at error level, counted
  (``symbol_error_count``, ``execution_events_total{type="fubon_symbol_unresolved"}``)
  and kept in a bounded ring (``unresolved_reports``) so it can be reconciled.
- **Order block**: ``ExecutionNormalizer.normalize_order`` reads price, side and
  quantities from a nested ``order`` mapping, so order reports carry one next to the
  flat keys (without it every order event came out with price 0 and quantity 0).
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.feed_adapter.fubon.order_codec import unwrap_sdk_result
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter.fubon.execution_callbacks")

# Default price scale factor (x10000).
PRICE_SCALE: int = 10_000

# Setters the SDK must expose for the execution path to be complete.
REQUIRED_SDK_SETTERS: tuple[str, ...] = (
    "set_on_futopt_order",
    "set_on_futopt_filled",
    "set_on_order_futopt_changed",
    "set_on_event",
)

_EVENT_CONTENT_LIMIT = 200

# Distinct (product, expiry, strike, call/put) keys kept by the symbol resolver.  A futures
# book needs a handful; options add one per strike, so the bound is generous but finite.
_SYMBOL_CACHE_MAX = 1024
# Dropped reports kept for reconciliation (identifiers only, no account data).
_UNRESOLVED_KEEP = 64
# ``execution_events_total`` label for a report dropped for want of a trading symbol.
_UNRESOLVED_METRIC_TYPE = "fubon_symbol_unresolved"

# ``function_type`` of an order report -> the ``op_type`` text ExecutionNormalizer
# understands.  Only codes that the SDK's own examples show are mapped (0 batch
# new order, 20 modify lot, 30 cancel); an unknown code leaves ``op_type`` empty.
_OP_TYPES: dict[int, str] = {0: "New", 20: "UpdateQty", 30: "Cancel"}


class FubonCallbackRegistrationError(RuntimeError):
    """The Fubon SDK cannot deliver execution reports (missing setter or bad handler)."""


class FubonSymbolConversionError(RuntimeError):
    """A report's product code could not be converted to the trading symbol."""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Retrieve *key* from a dict or object attribute."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _first(obj: Any, keys: tuple[str, ...]) -> Any:
    """Return the first present, non-empty value among *keys* (None if none)."""
    for key in keys:
        val = _get(obj, key)
        if val is not None and str(val).strip():
            return val
    return None


def _resolve_order_id(data: Any) -> str:
    """Extract order ID trying fields in priority: order_no > ord_no > order_id > seq_no."""
    val = _first(data, ("order_no", "ord_no", "order_id", "seq_no"))
    return "" if val is None else str(val)


def _resolve_side(data: Any) -> str:
    """Translate Fubon ``buy_sell`` (BSAction enum, ``"Buy"``, ``"B"``, ...) to ``Buy``/``Sell``."""
    raw = _get(data, "buy_sell", "")
    name = getattr(raw, "name", None)
    text = (name if isinstance(name, str) else str(raw)).rsplit(".", 1)[-1].strip()
    lowered = text.lower()
    if lowered in ("b", "buy"):
        return "Buy"
    if lowered in ("s", "sell"):
        return "Sell"
    return text


def _to_int(raw: Any) -> int:
    """Coerce a lot/quantity to int; 0 for None."""
    if raw is None:
        return 0
    return int(raw)


def _resolve_op_type(raw: Any) -> str:
    """Map a ``function_type`` code (int or numeric string) to an ``op_type`` text."""
    try:
        return _OP_TYPES.get(int(raw), "")
    except (TypeError, ValueError):
        return ""


def _scale_price(raw: Any) -> int:
    """Convert a raw price value to scaled int (x10000).

    Returns 0 for ``None`` or non-numeric values.

    NOTE: This function is NOT called in the callback paths (_on_sdk_order,
    _on_sdk_deal).  Scaling is delegated to ExecutionNormalizer (PriceCodec)
    to avoid double-scaling.  Kept here for any utility callers outside the
    hot path.
    """
    if raw is None:
        return 0
    try:
        return int(float(raw) * PRICE_SCALE)
    except (ValueError, TypeError):
        return 0


def bridge_order_callback(on_order: Callable[[Any, Any], None]) -> Callable[[dict[str, Any]], None]:
    """Adapt the platform's ``on_order(state, payload)`` to the adapter's ``on_order(payload)``.

    ``state`` is the SDK callback the report arrived through (``order`` or
    ``order_changed``), carried in the payload's ``source``.
    """

    def _forward(payload: dict[str, Any]) -> None:
        on_order(payload.get("source", ""), payload)

    return _forward


def _enum_text(value: Any) -> str:
    """Name of an enum member (``CallPut.Call`` -> ``Call``), else ``str(value)``."""
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value)


class FubonSymbolResolver:
    """Converts a report's product code + expiry to the trading symbol, with a bounded cache.

    ``sdk.futopt.convert_symbol(symbol, expiry_date[, strike_price, call_put])`` is the
    vendor's own mapping (``FITX`` + ``202612`` -> ``TXFL6``); the product-to-contract
    naming is not derivable locally, so nothing here invents a code.  Answers are cached
    per ``(symbol, expiry, strike, call_put)``, so the SDK is asked once per contract and
    a cache hit is a dict lookup.  The cache holds at most ``max_entries`` keys; the oldest
    is evicted first.  A failed conversion is never cached, so a later success is not
    shadowed.

    Reports arrive on SDK threads, so the cache is guarded by a lock; the SDK call itself
    runs outside it (two threads converting the same new key at once is harmless).
    """

    __slots__ = ("_sdk", "_cache", "_lock", "_max_entries")

    def __init__(self, sdk: Any, max_entries: int = _SYMBOL_CACHE_MAX) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._sdk = sdk
        self._cache: dict[tuple[str, str, str, str], str] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def __len__(self) -> int:
        return len(self._cache)

    def resolve(self, report: Any) -> str:
        """Return the trading symbol of an order / fill report.

        Raises:
            FubonSymbolConversionError: the report lacks the fields to convert, the SDK
                has no ``convert_symbol``, or it failed or returned something unusable.
        """
        product = _first(report, ("symbol",))
        expiry = _first(report, ("expiry_date",))
        if product is None or expiry is None:
            raise FubonSymbolConversionError("report carries no product symbol or expiry_date to convert")
        leg2 = _first(report, ("symbol_leg2",))
        if leg2 is not None:
            raise FubonSymbolConversionError(f"two-leg spread report ({product}/{leg2}) has no single trading symbol")
        call_put = _first(report, ("call_put",))
        strike = _get(report, "strike_price")
        is_option = call_put is not None
        if is_option and strike is None:
            raise FubonSymbolConversionError(f"option report {product} {expiry} carries no strike_price")
        key = (
            str(product),
            str(expiry),
            str(strike) if is_option else "",
            _enum_text(call_put) if is_option else "",
        )
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        trading = self._convert(str(product), str(expiry), strike, call_put, is_option)
        with self._lock:
            if key not in self._cache and len(self._cache) >= self._max_entries:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = trading
        logger.info("fubon_exec_cb.symbol_resolved", product=key[0], expiry=key[1], symbol=trading)
        return trading

    def _convert(self, product: str, expiry: str, strike: Any, call_put: Any, is_option: bool) -> str:
        convert = getattr(getattr(self._sdk, "futopt", None), "convert_symbol", None)
        if not callable(convert):
            raise FubonSymbolConversionError("sdk.futopt.convert_symbol is not available")
        try:
            answer = convert(product, expiry, strike, call_put) if is_option else convert(product, expiry)
            if hasattr(answer, "is_success"):
                answer = unwrap_sdk_result(answer, "convert_symbol")
        except Exception as exc:
            raise FubonSymbolConversionError(f"convert_symbol({product}, {expiry}) failed: {exc}") from exc
        if not isinstance(answer, str) or not answer.strip():
            raise FubonSymbolConversionError(f"convert_symbol({product}, {expiry}) returned {answer!r}")
        return answer.strip()


def _content_of(args: tuple[Any, ...]) -> tuple[Any, Any]:
    """Split an SDK delivery into ``(code, content)``; ``code`` is None for one argument."""
    if not args:
        return None, None
    if len(args) == 1:
        return None, args[0]
    return args[0], args[-1]


class FubonExecutionCallbackAdapter:
    """Translates Fubon SDK order/deal callbacks to the canonical format.

    Usage::

        adapter = FubonExecutionCallbackAdapter(fubon_sdk)
        adapter.register(on_order=my_order_handler, on_deal=my_deal_handler)

    ``register`` may be called again after a re-login; it re-wires the same
    setters and replaces the stored handlers.
    """

    __slots__ = (
        "_sdk",
        "_on_order",
        "_on_deal",
        "_on_event",
        "_order_buffer",
        "_deal_buffer",
        "_order_lock",
        "_deal_lock",
        "_error_count",
        "_symbols",
        "_unresolved",
        "_symbol_error_count",
        "log",
    )

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self._on_order: Callable[[dict[str, Any]], None] | None = None
        self._on_deal: Callable[[dict[str, Any]], None] | None = None
        self._on_event: Callable[[Any, Any], None] | None = None
        self._error_count: int = 0
        self._symbol_error_count: int = 0
        self._symbols = FubonSymbolResolver(sdk)
        self._unresolved: deque[dict[str, str]] = deque(maxlen=_UNRESOLVED_KEEP)
        self.log = logger
        self._order_lock = threading.Lock()
        self._deal_lock = threading.Lock()

        # Pre-allocated translation buffers (Allocator Law).
        # Values are overwritten on each callback -- the *same dict object*
        # is reused every time to avoid per-event heap allocation.
        # ``action`` repeats ``side``, ``order_no`` repeats ``order_id`` and ``custom_field`` repeats
        # ``strategy_id``: ExecutionNormalizer reads the side from ``action`` (a missing one is
        # read as BUY), the broker order number from ``order_no`` (FubonExecFieldMap) and the
        # order's tracking token from ``custom_field`` (``user_def`` is the token OrderAdapter
        # sent; ``_on_exec`` reads the same key straight off the payload).
        self._order_buffer: dict[str, Any] = {
            "order_id": "",
            "order_no": "",
            "seq_no": "",
            "symbol": "",
            "side": "",
            "action": "",
            "price": 0,
            "qty": 0,
            "status": "",
            "function_type": None,
            "op_type": "",
            "source": "",
            "strategy_id": "",
            "custom_field": "",
            "ts_ns": 0,
            "order": {},
        }
        self._deal_buffer: dict[str, Any] = {
            "order_id": "",
            "order_no": "",
            "seq_no": "",
            "fill_id": "",
            "symbol": "",
            "side": "",
            "action": "",
            "price": 0,
            "qty": 0,
            "strategy_id": "",
            "custom_field": "",
            "source_time": "",
            "ts_ns": 0,
        }

    @property
    def error_count(self) -> int:
        """Number of SDK deliveries that failed translation or the downstream handler."""
        return self._error_count

    @property
    def symbol_error_count(self) -> int:
        """Number of reports dropped because their trading symbol could not be resolved."""
        return self._symbol_error_count

    @property
    def unresolved_reports(self) -> tuple[dict[str, str], ...]:
        """Identifiers of the most recent dropped reports (bounded), oldest first."""
        return tuple(dict(entry) for entry in self._unresolved)

    def _drop_unresolved(self, kind: str, raw: Any, exc: Exception) -> None:
        """Record a report that must not be forwarded: its trading symbol is unknown."""
        self._error_count += 1
        self._symbol_error_count += 1
        ident = {
            "kind": kind,
            "order_no": _resolve_order_id(raw),
            "seq_no": "" if _get(raw, "seq_no") is None else str(_get(raw, "seq_no")),
            "filled_no": "" if _get(raw, "filled_no") is None else str(_get(raw, "filled_no")),
            "product": "" if _get(raw, "symbol") is None else str(_get(raw, "symbol")),
            "expiry_date": "" if _get(raw, "expiry_date") is None else str(_get(raw, "expiry_date")),
        }
        self._unresolved.append(ident)
        # Logged first so the loud signal survives a metrics failure; this runs on the SDK's
        # callback thread, where nothing may be raised.
        self.log.error("fubon_exec_cb.symbol_unresolved", error=str(exc), **ident)
        try:
            MetricsRegistry.get().execution_events_total.labels(type=_UNRESOLVED_METRIC_TYPE).inc()
        except Exception as metric_exc:
            self.log.warning("fubon_exec_cb.symbol_unresolved_metric_failed", error=str(metric_exc))

    # ------------------------------------------------------------------ #
    # Callback registration
    # ------------------------------------------------------------------ #

    def register(
        self,
        on_order: Callable[[dict[str, Any]], None],
        on_deal: Callable[[dict[str, Any]], None],
        on_event: Callable[[Any, Any], None] | None = None,
    ) -> None:
        """Wire order, deal and (optionally) event callbacks into the SDK.

        Raises:
            FubonCallbackRegistrationError: *on_order*/*on_deal* is not callable
                or the SDK lacks a required setter.  Nothing is wired then.
        """
        if not callable(on_order) or not callable(on_deal):
            raise FubonCallbackRegistrationError("on_order and on_deal must be callable")
        missing = [name for name in REQUIRED_SDK_SETTERS if not callable(getattr(self._sdk, name, None))]
        if missing:
            self.log.error("fubon_exec_cb.setters_missing", missing=missing)
            raise FubonCallbackRegistrationError(
                "Fubon SDK lacks required execution callback setter(s): " + ", ".join(missing)
            )

        self._on_order = on_order
        self._on_deal = on_deal
        self._on_event = on_event

        self._sdk.set_on_futopt_order(self._on_sdk_order)
        self._sdk.set_on_order_futopt_changed(self._on_sdk_order_changed)
        self._sdk.set_on_futopt_filled(self._on_sdk_deal)
        self._sdk.set_on_event(self._on_sdk_event)
        self.log.info("fubon_exec_cb.registered", setters=list(REQUIRED_SDK_SETTERS))

    # ------------------------------------------------------------------ #
    # SDK callbacks — translate Fubon → canonical
    # ------------------------------------------------------------------ #

    def _on_sdk_order(self, *args: Any) -> None:
        """``set_on_futopt_order`` delivery."""
        self._handle_order("order", args)

    def _on_sdk_order_changed(self, *args: Any) -> None:
        """``set_on_order_futopt_changed`` delivery (price/lot change, cancel)."""
        self._handle_order("order_changed", args)

    def _handle_order(self, source: str, args: tuple[Any, ...]) -> None:
        """Translate a Fubon order update to canonical format and forward."""
        handler = self._on_order
        if handler is None:
            return
        code, raw = _content_of(args)
        if raw is None:
            self._error_count += 1
            self.log.error("fubon_exec_cb.order_empty_content", source=source, code=str(code))
            return
        try:
            # Resolved before the buffer lock: a cache miss calls the SDK.
            symbol = self._symbols.resolve(raw)
            with self._order_lock:
                buf = self._order_buffer
                buf["order_id"] = buf["order_no"] = _resolve_order_id(raw)
                seq = _get(raw, "seq_no")
                buf["seq_no"] = "" if seq is None else str(seq)
                buf["symbol"] = symbol
                buf["side"] = buf["action"] = _resolve_side(raw)
                buf["price"] = _get(raw, "price")
                buf["qty"] = _to_int(_first(raw, ("lot", "quantity", "qty")))
                # The nested block ExecutionNormalizer.normalize_order reads price, side and
                # quantities from.  A fresh dict per report: the payload copy below is shallow.
                buf["order"] = {
                    "price": buf["price"],
                    "action": buf["action"],
                    "quantity": buf["qty"],
                    "filled_qty": _to_int(_get(raw, "filled_lot")),
                }
                buf["status"] = str(_get(raw, "status", ""))
                buf["function_type"] = _get(raw, "function_type")
                buf["op_type"] = _resolve_op_type(buf["function_type"])
                buf["source"] = source
                buf["strategy_id"] = buf["custom_field"] = str(_get(raw, "user_def", None) or "")
                buf["ts_ns"] = timebase.now_ns()
                # Shallow copy so the handler may schedule async work without
                # the next callback overwriting what it was handed.  Execution
                # reports are low frequency (~100 fills/day).
                payload = dict(buf)
            handler(payload)
        except FubonSymbolConversionError as exc:
            self._drop_unresolved("order", raw, exc)
        except Exception as exc:
            self._error_count += 1
            self.log.error("fubon_exec_cb.order_error", source=source, error=str(exc))

    def _on_sdk_deal(self, *args: Any) -> None:
        """``set_on_futopt_filled`` delivery: translate a fill and forward."""
        handler = self._on_deal
        if handler is None:
            return
        code, raw = _content_of(args)
        if raw is None:
            # The vendor passes an error code first and no content on failure.
            self._error_count += 1
            self.log.error("fubon_exec_cb.deal_empty_content", code=str(code))
            return
        try:
            symbol = self._symbols.resolve(raw)
            with self._deal_lock:
                buf = self._deal_buffer
                buf["order_id"] = buf["order_no"] = _resolve_order_id(raw)
                # ExecutionNormalizer takes ``seq_no`` as the fill id.  On a fill report
                # ``seq_no`` is the *order's* sequence, the same for every partial fill
                # of it, so it is joined with ``filled_no`` to keep fills distinct while a
                # redelivery of one fill still maps to the same id.
                seq = _get(raw, "seq_no")
                filled_no = _get(raw, "filled_no")
                if filled_no is not None and str(filled_no).strip():
                    fill_id = f"{'' if seq is None else seq}:{filled_no}"
                else:
                    fill_id = "" if seq is None else str(seq)
                buf["seq_no"] = buf["fill_id"] = fill_id
                buf["symbol"] = symbol
                buf["side"] = buf["action"] = _resolve_side(raw)
                buf["price"] = _first(raw, ("filled_price", "mat_price"))
                buf["qty"] = _to_int(_first(raw, ("filled_lots", "filled_lot", "mat_qty")))
                buf["strategy_id"] = buf["custom_field"] = str(_get(raw, "user_def", None) or "")
                buf["source_time"] = str(_get(raw, "filled_time", "") or "")
                buf["ts_ns"] = timebase.now_ns()
                payload = dict(buf)
            handler(payload)
        except FubonSymbolConversionError as exc:
            self._drop_unresolved("deal", raw, exc)
        except Exception as exc:
            self._error_count += 1
            self.log.error("fubon_exec_cb.deal_error", error=str(exc))

    def _on_sdk_event(self, *args: Any) -> None:
        """``set_on_event`` delivery: log it and forward to the optional handler."""
        code, content = _content_of(args)
        self.log.warning(
            "fubon_exec_cb.sdk_event",
            code=str(code),
            content=str(content)[:_EVENT_CONTENT_LIMIT],
        )
        handler = self._on_event
        if handler is None:
            return
        try:
            handler(code, content)
        except Exception as exc:
            self._error_count += 1
            self.log.error("fubon_exec_cb.event_error", error=str(exc))
