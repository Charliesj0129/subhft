"""Shadow Order Mode - logs orders without sending to broker."""

from __future__ import annotations

import os
from typing import Any

from structlog import get_logger

from hft_platform.contracts.strategy import OrderIntent
from hft_platform.core import timebase
from hft_platform.order.shadow_writer import ShadowOrderWriter

logger = get_logger("order.shadow")


def _get_metrics() -> Any:
    """Lazy import MetricsRegistry to avoid circular imports."""
    try:
        from hft_platform.observability.metrics import MetricsRegistry

        return MetricsRegistry.get()
    except Exception:  # noqa: BLE001
        return None


class ShadowOrderSink:
    """Intercepts orders for shadow logging without broker execution."""

    __slots__ = ("_enabled", "_counter", "_writer")

    def __init__(self, enabled: bool | None = None, writer: ShadowOrderWriter | None = None):
        if enabled is not None:
            self._enabled = enabled
        else:
            self._enabled = os.getenv("HFT_ORDER_SHADOW_MODE", "0") == "1"
        self._counter = 0
        self._writer = writer
        # Set shadow_mode_active gauge
        metrics = _get_metrics()
        if metrics and hasattr(metrics, "shadow_mode_active"):
            metrics.shadow_mode_active.set(1 if self._enabled else 0)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def counter(self) -> int:
        return self._counter

    def intercept(self, intent: OrderIntent) -> dict:
        """Log the order and return a record dict."""
        self._counter += 1
        record = {
            "ts_ns": timebase.now_ns(),
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
            "side": str(intent.side.name if hasattr(intent.side, "name") else intent.side),
            "price": intent.price,
            "qty": intent.qty,
            "intent_type": str(intent.intent_type.name if hasattr(intent.intent_type, "name") else intent.intent_type),
            "intent_id": str(intent.intent_id),
            "shadow": True,
        }
        # DEBUG level: shadow-intercept fires once per intent (~8 k/day for R47).
        # The `shadow_orders_total` Prometheus counter below is the liveness signal;
        # per-intent logging is only needed for DLQ/forensic replay.
        logger.debug("Shadow order captured", **record)
        # Emit Prometheus metrics
        metrics = _get_metrics()
        if metrics and hasattr(metrics, "shadow_orders_total"):
            side_str = str(intent.side.name if hasattr(intent.side, "name") else intent.side)
            capped_sym = metrics.cap_symbol(intent.symbol) if hasattr(metrics, "cap_symbol") else intent.symbol
            metrics.shadow_orders_total.labels(
                strategy=intent.strategy_id,
                symbol=capped_sym,
                side=side_str,
            ).inc()
        if self._writer is not None:
            self._writer.add(record)
        return record

    def flush(self) -> None:
        """Flush any pending records to ClickHouse via the writer."""
        if self._writer is not None:
            self._writer.flush()
