"""Fubon execution callback adapter.

Translates Fubon SDK active-order and deal reporting into the platform's
canonical callback interface so downstream consumers (ExecutionNormalizer,
PositionTracker, etc.) operate without broker awareness.

Design notes
------------
- **Allocator Law**: Translation buffers (``_order_buffer``, ``_deal_buffer``)
  are pre-allocated once in ``__init__`` and reused by overwriting values in
  each callback invocation.  No per-event heap allocation.
- **Precision Law**: Float prices from Fubon are converted to scaled integers
  (x10000) at this boundary so downstream consumers never see floats.
- **Boundary Law**: All Fubon-specific field names are translated to canonical
  keys (``order_id``, ``symbol``, ``side``, ``price``, ``qty``, etc.) at this
  boundary.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from structlog import get_logger

logger = get_logger("feed_adapter.fubon.execution_callbacks")

# Default price scale factor (x10000).
PRICE_SCALE: int = 10_000


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Retrieve *key* from a dict or object attribute."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _resolve_order_id(data: Any) -> str:
    """Extract order ID trying fields in priority: ord_no > order_id > seq_no."""
    for field in ("ord_no", "order_id", "seq_no"):
        val = _get(data, field)
        if val is not None and str(val).strip():
            return str(val)
    return ""


def _resolve_side(data: Any) -> str:
    """Translate Fubon buy_sell field to canonical side string."""
    raw = str(_get(data, "buy_sell", ""))
    if raw in ("B", "Buy"):
        return "Buy"
    if raw in ("S", "Sell"):
        return "Sell"
    return raw


def _scale_price(raw: Any) -> int:
    """Convert a raw price value to scaled int (x10000).

    Returns 0 for ``None`` or non-numeric values.
    """
    if raw is None:
        return 0
    try:
        return int(float(raw) * PRICE_SCALE)
    except (ValueError, TypeError):
        return 0


class FubonExecutionCallbackAdapter:
    """Translates Fubon SDK order/deal callbacks to the canonical format.

    Usage::

        adapter = FubonExecutionCallbackAdapter(fubon_sdk)
        adapter.register(on_order=my_order_handler, on_deal=my_deal_handler)
    """

    __slots__ = (
        "_sdk",
        "_on_order",
        "_on_deal",
        "_order_buffer",
        "_deal_buffer",
        "log",
    )

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self._on_order: Callable[[dict[str, Any]], None] | None = None
        self._on_deal: Callable[[dict[str, Any]], None] | None = None
        self.log = logger

        # Pre-allocated translation buffers (Allocator Law).
        # Values are overwritten on each callback -- the *same dict object*
        # is reused every time to avoid per-event heap allocation.
        self._order_buffer: dict[str, Any] = {
            "order_id": "",
            "symbol": "",
            "side": "",
            "price": 0,
            "qty": 0,
            "status": "",
            "strategy_id": "",
            "ts_ns": 0,
        }
        self._deal_buffer: dict[str, Any] = {
            "order_id": "",
            "symbol": "",
            "side": "",
            "deal_price": 0,
            "deal_qty": 0,
            "strategy_id": "",
            "ts_ns": 0,
        }

    # ------------------------------------------------------------------ #
    # Callback registration
    # ------------------------------------------------------------------ #

    def register(
        self,
        on_order: Callable[[dict[str, Any]], None],
        on_deal: Callable[[dict[str, Any]], None],
    ) -> None:
        """Wire order and deal callbacks.

        If the SDK exposes ``set_order_callback`` / ``set_on_dealt``, they
        are wired automatically.  Otherwise the callbacks are stored for
        manual polling by the caller.
        """
        self._on_order = on_order
        self._on_deal = on_deal

        # Attempt SDK-level callback registration.
        if hasattr(self._sdk, "set_order_callback"):
            self._sdk.set_order_callback(self._on_sdk_order)
            self.log.info("fubon_exec_cb.sdk_order_callback_wired")

        if hasattr(self._sdk, "set_on_dealt"):
            self._sdk.set_on_dealt(self._on_sdk_deal)
            self.log.info("fubon_exec_cb.sdk_deal_callback_wired")

        self.log.info("fubon_exec_cb.registered")

    # ------------------------------------------------------------------ #
    # SDK callbacks — translate Fubon → canonical
    # ------------------------------------------------------------------ #

    def _on_sdk_order(self, raw: Any) -> None:
        """Translate a Fubon order update to canonical format and forward."""
        if self._on_order is None:
            return
        try:
            buf = self._order_buffer
            buf["order_id"] = _resolve_order_id(raw)
            buf["symbol"] = str(_get(raw, "stock_no", ""))
            buf["side"] = _resolve_side(raw)
            buf["price"] = _scale_price(_get(raw, "price"))
            buf["qty"] = int(_get(raw, "qty", 0) or 0)
            buf["status"] = str(_get(raw, "status", ""))
            buf["strategy_id"] = str(_get(raw, "user_def", ""))
            buf["ts_ns"] = time.perf_counter_ns()

            self._on_order(buf)
        except Exception as exc:
            self.log.error("fubon_exec_cb.order_error", error=str(exc))

    def _on_sdk_deal(self, raw: Any) -> None:
        """Translate a Fubon deal/fill to canonical format and forward."""
        if self._on_deal is None:
            return
        try:
            buf = self._deal_buffer
            buf["order_id"] = _resolve_order_id(raw)
            buf["symbol"] = str(_get(raw, "stock_no", ""))
            buf["side"] = _resolve_side(raw)
            buf["deal_price"] = _scale_price(_get(raw, "mat_price"))
            buf["deal_qty"] = int(_get(raw, "mat_qty", 0) or 0)
            buf["strategy_id"] = str(_get(raw, "user_def", ""))
            buf["ts_ns"] = time.perf_counter_ns()

            self._on_deal(buf)
        except Exception as exc:
            self.log.error("fubon_exec_cb.deal_error", error=str(exc))
