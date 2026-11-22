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
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase

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

# ``function_type`` of an order report -> the ``op_type`` text ExecutionNormalizer
# understands.  Only codes that the SDK's own examples show are mapped (0 batch
# new order, 20 modify lot, 30 cancel); an unknown code leaves ``op_type`` empty.
_OP_TYPES: dict[int, str] = {0: "New", 20: "UpdateQty", 30: "Cancel"}


class FubonCallbackRegistrationError(RuntimeError):
    """The Fubon SDK cannot deliver execution reports (missing setter or bad handler)."""


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
        "log",
    )

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self._on_order: Callable[[dict[str, Any]], None] | None = None
        self._on_deal: Callable[[dict[str, Any]], None] | None = None
        self._on_event: Callable[[Any, Any], None] | None = None
        self._error_count: int = 0
        self.log = logger
        self._order_lock = threading.Lock()
        self._deal_lock = threading.Lock()

        # Pre-allocated translation buffers (Allocator Law).
        # Values are overwritten on each callback -- the *same dict object*
        # is reused every time to avoid per-event heap allocation.
        # ``action`` repeats ``side`` and ``order_no`` repeats ``order_id``: ExecutionNormalizer
        # reads the side from ``action`` (a missing one is read as BUY) and the
        # broker order number from ``order_no`` (FubonExecFieldMap).
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
            "ts_ns": 0,
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
            "source_time": "",
            "ts_ns": 0,
        }

    @property
    def error_count(self) -> int:
        """Number of SDK deliveries that failed translation or the downstream handler."""
        return self._error_count

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
            with self._order_lock:
                buf = self._order_buffer
                buf["order_id"] = buf["order_no"] = _resolve_order_id(raw)
                seq = _get(raw, "seq_no")
                buf["seq_no"] = "" if seq is None else str(seq)
                symbol = _first(raw, ("symbol", "stock_no"))
                buf["symbol"] = "" if symbol is None else str(symbol)
                buf["side"] = buf["action"] = _resolve_side(raw)
                buf["price"] = _get(raw, "price")
                buf["qty"] = _to_int(_first(raw, ("lot", "quantity", "qty")))
                buf["status"] = str(_get(raw, "status", ""))
                buf["function_type"] = _get(raw, "function_type")
                buf["op_type"] = _resolve_op_type(buf["function_type"])
                buf["source"] = source
                buf["strategy_id"] = str(_get(raw, "user_def", None) or "")
                buf["ts_ns"] = timebase.now_ns()
                # Shallow copy so the handler may schedule async work without
                # the next callback overwriting what it was handed.  Execution
                # reports are low frequency (~100 fills/day).
                payload = dict(buf)
            handler(payload)
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
                symbol = _first(raw, ("symbol", "stock_no"))
                buf["symbol"] = "" if symbol is None else str(symbol)
                buf["side"] = buf["action"] = _resolve_side(raw)
                buf["price"] = _first(raw, ("filled_price", "mat_price"))
                buf["qty"] = _to_int(_first(raw, ("filled_lots", "filled_lot", "mat_qty")))
                buf["strategy_id"] = str(_get(raw, "user_def", None) or "")
                buf["source_time"] = str(_get(raw, "filled_time", "") or "")
                buf["ts_ns"] = timebase.now_ns()
                payload = dict(buf)
            handler(payload)
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
