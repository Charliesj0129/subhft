"""Factories for order/risk objects used across the test suite.

All prices use scaled integers (x10000) per the Precision Law.
Timestamps use ``timebase.now_ns()`` per project convention.
"""

from __future__ import annotations

from typing import Any

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase


def make_order_intent(
    intent_id: int = 1,
    *,
    strategy_id: str = "s1",
    symbol: str = "2330",
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
    price: int = 5_000_000,
    qty: int = 1,
    tif: TIF = TIF.LIMIT,
    target_order_id: str | None = None,
    timestamp_ns: int = 0,
    source_ts_ns: int = 0,
    reason: str = "",
    trace_id: str = "",
    idempotency_key: str = "",
    ttl_ns: int = 0,
) -> OrderIntent:
    """Create an ``OrderIntent`` with sensible defaults.

    ``price`` is scaled x10000 (default 5_000_000 = 500.0 TWD).
    ``idempotency_key`` defaults to ``"key-{intent_id}"`` when empty.
    """
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=tif,
        target_order_id=target_order_id,
        timestamp_ns=timestamp_ns or timebase.now_ns(),
        source_ts_ns=source_ts_ns,
        reason=reason,
        trace_id=trace_id,
        idempotency_key=idempotency_key or f"key-{intent_id}",
        ttl_ns=ttl_ns,
    )


def make_order_command(
    cmd_id: int = 1,
    *,
    intent: OrderIntent | None = None,
    deadline_ns: int = 0,
    storm_guard_state: StormGuardState = StormGuardState.NORMAL,
    created_ns: int = 0,
    **intent_kwargs: Any,
) -> OrderCommand:
    """Create an ``OrderCommand`` with sensible defaults.

    If ``intent`` is not provided, one is built via ``make_order_intent``
    using any extra ``**intent_kwargs``.

    ``deadline_ns`` defaults to ``now + 500ms``.
    """
    now = timebase.now_ns()
    if intent is None:
        intent = make_order_intent(**intent_kwargs)
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=deadline_ns or (now + 500_000_000),
        storm_guard_state=storm_guard_state,
        created_ns=created_ns or now,
    )


def make_risk_config(
    *,
    max_notional: int = 500_000_000,
    per_symbol_max_notional: int = 5_000_000_000,
    max_position_lots: int = 2,
    max_daily_loss: int = 10_000_000,
    max_order_size: int = 2,
    max_price_cap: int = 50_000_000,
    strategies: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a risk config dict matching ``strategy_limits.yaml`` schema.

    All monetary values are scaled x10000.

    Defaults mirror ``config/base/strategy_limits.yaml``:
    - ``max_notional``: 500_000_000 (50K NTD x10000)
    - ``max_daily_loss``: 10_000_000 (1K NTD x10000)
    - ``max_price_cap``: 50_000_000 (5K NTD x10000)
    """
    config: dict[str, Any] = {
        "global_limits": {
            "max_position_notional": max_notional,
            "max_order_size": max_order_size,
            "max_daily_loss": max_daily_loss,
        },
        "global_defaults": {
            "max_notional": max_notional,
            "per_symbol_max_notional": per_symbol_max_notional,
            "max_position_lots": max_position_lots,
            "max_daily_loss": max_daily_loss,
            "max_price_cap": max_price_cap,
        },
        "strategies": strategies or {},
    }
    return config
