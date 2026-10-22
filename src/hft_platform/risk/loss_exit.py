"""Close positions the platform's own stops would otherwise freeze.

A stop that blocks new orders does not reduce risk while a position is open: on
THESHOW the daily-loss HALT held one TMFJ6 long from 10/15 to its settlement on
10/21. HALT blocked new orders, R47's own spread gate suppressed its exit quotes,
and ``HaltFlattener`` was never constructed, so nothing could close the lot and
the exchange settled it. This controller is the exit those stops lacked. It runs
on the supervisor tick (1 Hz, off the hot path) and emits ``FORCE_FLAT`` intents
into the normal risk queue for any position that meets one of three triggers:

``daily_loss_halt``
    The daily-loss validator has latched its stop and the position is open.
``position_loss``
    The position's loss since entry reached ``position_loss_limit_ntd``. The daily
    limit now judges only the day's change (``DailyLossLimitValidator.daily_unrealized``),
    so this is what bounds a loss carried across days.
``expiry``
    It is the contract's delivery date and past the cutoff (``risk/expiry.py``);
    ``ExpiringContractValidator`` stops new lots in it from the same moment.

Orders are marketable limits, never price 0: ``protection_ticks`` through the
opposite best price. A position with no two-sided book is left alone and
retried, because there is no price to send.

One intent per symbol. ``OrderAdapter`` dispatches a FORCE_FLAT for the
platform's whole net position in the symbol, on the closing side, IOC, at the
intent's price; it ignores the intent's side and quantity. The intent carries
the ``strategy_id`` holding the largest share of the symbol so the fill reduces
that strategy's position (a separate platform id would open an offsetting
position under another key instead). With more than one holder the fill is
booked to that one strategy; ``loss_exit_shared_symbol`` says so.

Residual: the owning strategy also receives the order's ``on_order`` / ``on_fill``
events. R47 releases a pending slot on each, so after a ``position_loss`` exit
it may hold one more working order than its own count says until its next
terminal event; the position limit validator still caps the filled position.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from prometheus_client import Counter
from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.risk.expiry import DEFAULT_EXPIRY_CUTOFF_MINUTE, expiry_window_ns

logger = get_logger("risk.loss_exit")

loss_exit_orders_total = Counter(
    "loss_exit_orders_total",
    "FORCE_FLAT intents emitted by the platform loss/expiry exit, by trigger",
    ["trigger"],
)
loss_exit_unpriced_total = Counter(
    "loss_exit_unpriced_total",
    "Exit triggers that could not be priced (no two-sided book), by trigger",
    ["trigger"],
)

TRIGGER_DAILY_LOSS_HALT = "daily_loss_halt"
TRIGGER_POSITION_LOSS = "position_loss"
TRIGGER_EXPIRY = "expiry"

# Intent ids for platform exits start here so their order keys
# (``strategy_id:intent_id``) cannot collide with a strategy's own counter,
# which restarts daily and has reached ~50,000 on THESHOW. Below 2**31.
INTENT_ID_BASE = 1_900_000_000


@dataclass(slots=True, frozen=True)
class OpenPosition:
    """One strategy's net position in one symbol (one position-store entry), as the exit sees it."""

    strategy_id: str
    symbol: str
    net_qty: int
    avg_price_scaled: int


class LossExitController:
    """Plans FORCE_FLAT intents for positions past a loss or expiry trigger.

    ``plan()`` is pure apart from its retry bookkeeping: the caller supplies the
    positions, the book and the stop state, and submits what comes back.
    """

    __slots__ = (
        "_protection_ticks",
        "_retry_interval_ns",
        "_last_submit_ns",
        "_next_intent_id",
        "_window_ns_cache",
    )

    def __init__(self, *, protection_ticks: int = 5, retry_interval_s: float = 10.0) -> None:
        self._protection_ticks = max(0, int(protection_ticks))
        self._retry_interval_ns = int(retry_interval_s * 1_000_000_000)
        # symbol -> when an exit was last submitted for it.
        self._last_submit_ns: dict[str, int] = {}
        self._next_intent_id = INTENT_ID_BASE
        self._window_ns_cache: dict[tuple[str, int], tuple[int, int]] = {}

    def _expiry_passed(self, symbol: str, now_ns: int, cutoff_minute: int) -> bool:
        key = (symbol, cutoff_minute)
        window = self._window_ns_cache.get(key)
        if window is None:
            window = expiry_window_ns(symbol, now_ns, cutoff_minute)
            self._window_ns_cache[key] = window
        return window[0] <= now_ns < window[1]

    def plan(
        self,
        *,
        now_ns: int,
        positions: Iterable[OpenPosition],
        daily_halt: bool,
        position_loss_limit_scaled: int,
        expiry_enabled: bool,
        l1: Callable[[str], tuple[int, int] | None],
        multiplier: Callable[[str], int],
        tick_size: Callable[[str], int],
        expiry_cutoff_minute: int = DEFAULT_EXPIRY_CUTOFF_MINUTE,
    ) -> list[OrderIntent]:
        """Return the exit intents due now.

        ``l1(symbol)`` gives ``(best_bid, best_ask)`` scaled, or None.
        ``position_loss_limit_scaled`` of 0 disables that trigger.
        """
        # symbol -> [net_qty, signed qty x entry, {strategy_id: net_qty}]
        by_symbol: dict[str, list] = {}
        for pos in positions:
            if pos.net_qty == 0:
                continue
            acc = by_symbol.setdefault(pos.symbol, [0, 0, {}])
            acc[0] += pos.net_qty
            acc[1] += pos.net_qty * pos.avg_price_scaled
            holders = acc[2]
            holders[pos.strategy_id] = holders.get(pos.strategy_id, 0) + pos.net_qty

        intents: list[OrderIntent] = []
        due: set[str] = set()
        for symbol, (net_qty, signed_cost, holders) in by_symbol.items():
            if net_qty == 0:
                continue
            strategy_id = max(holders, key=lambda sid: abs(holders[sid]))
            book = l1(symbol)
            bid, ask = book if book is not None else (0, 0)
            priced = bid > 0 and ask > 0
            trigger = self._trigger(
                symbol=symbol,
                net_qty=net_qty,
                avg_price_scaled=signed_cost // net_qty,
                now_ns=now_ns,
                daily_halt=daily_halt,
                position_loss_limit_scaled=position_loss_limit_scaled,
                expiry_enabled=expiry_enabled,
                expiry_cutoff_minute=expiry_cutoff_minute,
                mid=(bid + ask) // 2 if priced else 0,
                multiplier=multiplier,
            )
            if trigger is None:
                continue
            key = symbol
            due.add(key)
            last = self._last_submit_ns.get(key)
            if last is not None and now_ns - last < self._retry_interval_ns:
                continue
            if not priced:
                loss_exit_unpriced_total.labels(trigger=trigger).inc()
                logger.warning("loss_exit_unpriced", strategy_id=strategy_id, symbol=symbol, trigger=trigger)
                continue
            if sum(1 for q in holders.values() if q != 0) > 1:
                logger.warning("loss_exit_shared_symbol", symbol=symbol, holders=dict(holders), booked_to=strategy_id)
            intents.append(self._build(strategy_id, symbol, net_qty, bid, ask, tick_size(symbol), trigger, now_ns))
            self._last_submit_ns[key] = now_ns
            loss_exit_orders_total.labels(trigger=trigger).inc()
        # A position that no longer triggers (closed, or recovered) starts fresh next time.
        for key in [k for k in self._last_submit_ns if k not in due]:
            del self._last_submit_ns[key]
        return intents

    def _trigger(
        self,
        *,
        symbol: str,
        net_qty: int,
        avg_price_scaled: int,
        now_ns: int,
        daily_halt: bool,
        position_loss_limit_scaled: int,
        expiry_enabled: bool,
        expiry_cutoff_minute: int,
        mid: int,
        multiplier: Callable[[str], int],
    ) -> str | None:
        if daily_halt:
            return TRIGGER_DAILY_LOSS_HALT
        if expiry_enabled and self._expiry_passed(symbol, now_ns, expiry_cutoff_minute):
            return TRIGGER_EXPIRY
        if position_loss_limit_scaled > 0 and mid > 0:
            # Same formula as MarkToMarketCalculator._unrealized: NTD x price scale.
            unrealized = (mid - avg_price_scaled) * net_qty * multiplier(symbol)
            if unrealized <= -position_loss_limit_scaled:
                return TRIGGER_POSITION_LOSS
        return None

    def _build(
        self,
        strategy_id: str,
        symbol: str,
        net_qty: int,
        bid: int,
        ask: int,
        tick: int,
        trigger: str,
        now_ns: int,
    ) -> OrderIntent:
        self._next_intent_id += 1
        through = self._protection_ticks * max(tick, 0)
        if net_qty > 0:
            side, price = Side.SELL, max(bid - through, max(tick, 1))
        else:
            side, price = Side.BUY, ask + through
        intent_id = self._next_intent_id
        logger.warning(
            "loss_exit_emit",
            strategy_id=strategy_id,
            symbol=symbol,
            trigger=trigger,
            side=side.name,
            qty=abs(net_qty),
            price=price,
            best_bid=bid,
            best_ask=ask,
            intent_id=intent_id,
        )
        return OrderIntent(
            intent_id=intent_id,
            strategy_id=strategy_id,
            symbol=symbol,
            intent_type=IntentType.FORCE_FLAT,
            side=side,
            price=price,
            qty=abs(net_qty),
            tif=TIF.IOC,
            timestamp_ns=now_ns,
            source_ts_ns=now_ns,
            reason=f"loss_exit:{trigger}",
            idempotency_key=f"loss_exit:{strategy_id}:{symbol}:{intent_id}",
            decision_price=(bid + ask) // 2,
        )
