"""
Shared fixtures and helpers for the E2E test suite (7-plane coverage).

All prices use scaled integers (x10000) per the HFT Precision Law.
All timestamps are nanoseconds.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import numpy as np
import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData, TickEvent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCALE: int = 10_000
DEFAULT_SYMBOL: str = "2330"
DEFAULT_PRICE: int = 500 * SCALE  # 5_000_000
DEFAULT_TS_NS: int = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: end-to-end tests covering full runtime planes")
    config.addinivalue_line("markers", "e2e_chain: multi-step chained E2E scenarios")
    config.addinivalue_line("markers", "e2e_integration: E2E tests requiring external services (ClickHouse, Redis)")


# ---------------------------------------------------------------------------
# InMemoryBrokerAPI
# ---------------------------------------------------------------------------


class InMemoryBrokerAPI:
    """
    Minimal in-memory broker stub for E2E tests.

    Tracks placed/cancelled orders and provides predictable seq/ord_no values
    without requiring a real broker SDK.
    """

    mode: str = "sim"
    logged_in: bool = True

    def __init__(self) -> None:
        self.placed_orders: list[dict[str, Any]] = []
        self.cancelled_orders: list[dict[str, Any]] = []
        self.last_trade: dict[str, Any] | None = None
        self.should_reject: bool = False
        self._seq_counter: int = 1000
        self._ord_counter: int = 1

    def get_exchange(self, symbol: str) -> str:  # noqa: ARG002
        return "TSE"

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        if self.should_reject:
            raise RuntimeError(f"InMemoryBrokerAPI: order rejected (should_reject=True): {kwargs}")
        self._seq_counter += 1
        self._ord_counter += 1
        trade: dict[str, Any] = {
            "seqno": str(self._seq_counter),
            "ord_no": f"A{self._ord_counter:06d}",
            **kwargs,
        }
        self.placed_orders.append(trade)
        self.last_trade = trade
        return trade

    def cancel_order(self, trade: dict[str, Any]) -> dict[str, Any]:
        self.cancelled_orders.append(trade)
        return trade

    def update_order(self, trade: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        updated = {**trade, **kwargs}
        self.placed_orders.append(updated)
        self.last_trade = updated
        return updated


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def broker_api() -> InMemoryBrokerAPI:
    """Fresh InMemoryBrokerAPI for each test."""
    return InMemoryBrokerAPI()


@pytest.fixture()
def bounded_queues() -> dict[str, asyncio.Queue]:  # type: ignore[type-arg]
    """Five bounded asyncio queues representing the inter-service bus."""
    return {
        "raw_queue": asyncio.Queue(maxsize=64),
        "raw_exec_queue": asyncio.Queue(maxsize=64),
        "risk_queue": asyncio.Queue(maxsize=64),
        "order_queue": asyncio.Queue(maxsize=64),
        "recorder_queue": asyncio.Queue(maxsize=64),
    }


@pytest.fixture()
def e2e_symbols_yaml(tmp_path: Any) -> str:
    """Write a minimal symbols.yaml and return its path."""
    content = """\
symbols:
  - symbol: "2330"
    exchange: TSE
    scale: 10000
    lot_size: 1000
    tick_size: 1
  - symbol: "TXFD6"
    exchange: TAIFEX
    scale: 10000
    lot_size: 1
    tick_size: 10000
  - symbol: "TMFD6"
    exchange: TAIFEX
    scale: 10000
    lot_size: 1
    tick_size: 10000
"""
    path = tmp_path / "symbols.yaml"
    path.write_text(content)
    return str(path)


@pytest.fixture()
def e2e_risk_yaml(tmp_path: Any) -> str:
    """Write a minimal strategy_limits.yaml and return its path."""
    content = """\
default:
  max_position: 10000
  max_order_qty: 1000
  max_daily_loss: 500000000
  max_open_orders: 20
  max_notional: 100000000000
strategies:
  test_strategy:
    max_position: 5000
    max_order_qty: 500
    max_daily_loss: 100000000
    max_open_orders: 10
    max_notional: 50000000000
"""
    path = tmp_path / "strategy_limits.yaml"
    path.write_text(content)
    return str(path)


@pytest.fixture()
def e2e_adapter_yaml(tmp_path: Any) -> str:
    """Write a minimal adapter config and return its path."""
    content = """\
adapter:
  mode: sim
  order_timeout_ns: 5000000000
  max_retries: 3
  retry_backoff_ms: 100
  queue_maxsize: 64
"""
    path = tmp_path / "adapter.yaml"
    path.write_text(content)
    return str(path)


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def wait_for_predicate(
    predicate: Callable[[], bool],
    timeout: float = 2.0,
    step: float = 0.01,
) -> bool:
    """
    Poll *predicate* every *step* seconds until it returns True or *timeout* elapses.

    Returns True if predicate became True within the timeout, False otherwise.
    """
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(step)
        elapsed += step
    return predicate()


async def collect_bus_events(
    bus: asyncio.Queue,  # type: ignore[type-arg]
    count: int,
    timeout: float = 2.0,
) -> list[Any]:
    """
    Drain up to *count* events from *bus* within *timeout* seconds.

    Returns a list of collected events (may be fewer than *count* on timeout).
    """
    collected: list[Any] = []
    deadline = asyncio.get_event_loop().time() + timeout
    while len(collected) < count:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(bus.get(), timeout=remaining)
            collected.append(event)
        except asyncio.TimeoutError:
            break
    return collected


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def make_intent(
    intent_id: int = 1,
    strategy_id: str = "test_strategy",
    symbol: str = DEFAULT_SYMBOL,
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
    price: int = DEFAULT_PRICE,
    qty: int = 1,
    tif: TIF = TIF.LIMIT,
    timestamp_ns: int = DEFAULT_TS_NS,
    **kwargs: Any,
) -> OrderIntent:
    """Build an OrderIntent with sensible defaults."""
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=tif,
        timestamp_ns=timestamp_ns,
        **kwargs,
    )


def make_command(
    cmd_id: int = 1,
    intent: OrderIntent | None = None,
    deadline_ns: int = DEFAULT_TS_NS + 5_000_000_000,
    storm_guard_state: StormGuardState = StormGuardState.NORMAL,
    created_ns: int = DEFAULT_TS_NS,
    **kwargs: Any,
) -> OrderCommand:
    """Build an OrderCommand with sensible defaults."""
    if intent is None:
        intent = make_intent()
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=deadline_ns,
        storm_guard_state=storm_guard_state,
        created_ns=created_ns,
        **kwargs,
    )


def make_tick(
    symbol: str = DEFAULT_SYMBOL,
    price: int = DEFAULT_PRICE,
    volume: int = 100,
    seq: int = 1,
    source_ts: int = DEFAULT_TS_NS,
    local_ts: int = DEFAULT_TS_NS,
    **kwargs: Any,
) -> TickEvent:
    """Build a TickEvent with sensible defaults."""
    meta = MetaData(seq=seq, source_ts=source_ts, local_ts=local_ts)
    return TickEvent(
        meta=meta,
        symbol=symbol,
        price=price,
        volume=volume,
        **kwargs,
    )


def make_bidask(
    symbol: str = DEFAULT_SYMBOL,
    bid_price: int = DEFAULT_PRICE - SCALE,  # 499 * SCALE
    ask_price: int = DEFAULT_PRICE + SCALE,  # 501 * SCALE
    bid_vol: int = 500,
    ask_vol: int = 500,
    seq: int = 1,
    source_ts: int = DEFAULT_TS_NS,
    local_ts: int = DEFAULT_TS_NS,
    **kwargs: Any,
) -> BidAskEvent:
    """Build a BidAskEvent with sensible defaults."""
    meta = MetaData(seq=seq, source_ts=source_ts, local_ts=local_ts)
    bids = np.array([[bid_price, bid_vol]], dtype=np.int64)
    asks = np.array([[ask_price, ask_vol]], dtype=np.int64)
    return BidAskEvent(
        meta=meta,
        symbol=symbol,
        bids=bids,
        asks=asks,
        **kwargs,
    )


def make_fill(
    fill_id: str = "fill-001",
    account_id: str = "test-account",
    order_id: str = "ord-001",
    strategy_id: str = "test_strategy",
    symbol: str = DEFAULT_SYMBOL,
    side: Side = Side.BUY,
    qty: int = 1,
    price: int = DEFAULT_PRICE,
    fee: int = 1000,
    tax: int = 500,
    ingest_ts_ns: int = DEFAULT_TS_NS,
    match_ts_ns: int = DEFAULT_TS_NS,
    **kwargs: Any,
) -> FillEvent:
    """Build a FillEvent with sensible defaults."""
    return FillEvent(
        fill_id=fill_id,
        account_id=account_id,
        order_id=order_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=ingest_ts_ns,
        match_ts_ns=match_ts_ns,
        **kwargs,
    )


def make_lob_stats(
    symbol: str = DEFAULT_SYMBOL,
    ts: int = DEFAULT_TS_NS,
    best_bid: int = DEFAULT_PRICE - SCALE,
    best_ask: int = DEFAULT_PRICE + SCALE,
    bid_depth: int = 500,
    ask_depth: int = 500,
    imbalance: float = 0.0,
    **kwargs: Any,
) -> LOBStatsEvent:
    """Build a LOBStatsEvent with sensible defaults."""
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        **kwargs,
    )
