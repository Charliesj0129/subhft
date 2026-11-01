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
    so this is what bounds a loss carried across days. Skipped for a symbol
    holding a lot recovered with no entry price, whose loss is unknown.
``expiry``
    It is the contract's delivery date and past the cutoff (``risk/expiry.py``);
    ``ExpiringContractValidator`` stops new lots in it from the same moment.
``operator_flatten``
    An operator asked for it (``hft ops flatten``); the supervisor names the
    symbols until they are flat or the request's deadline passes.
``kill_switch``
    The kill switch is set and the last reconciliation, recent enough, found
    the platform's positions equal to the broker's. Without that evidence the
    supervisor sends nothing and raises ``kill_switch_flatten_blocked``: a
    closing order computed from a book that may be wrong can open a position.

Both also cancel resting orders (``cancel_intents``): every order for the kill
switch, the named symbols' for an operator flatten, so an order left at the
exchange cannot fill after the book is flat and reopen it.

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

from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.expiry import DEFAULT_EXPIRY_CUTOFF_MINUTE, expiry_window_ns

logger = get_logger("risk.loss_exit")

TRIGGER_DAILY_LOSS_HALT = "daily_loss_halt"
TRIGGER_POSITION_LOSS = "position_loss"
TRIGGER_EXPIRY = "expiry"
TRIGGER_OPERATOR = "operator_flatten"
TRIGGER_KILL_SWITCH = "kill_switch"

# Books an exit for a position that names no strategy (a legacy recovery entry).
# Its fill seeds the recovered lot under this id and closes it.
UNATTRIBUTED_STRATEGY_ID = "__loss_exit__"

# Intent ids for platform exits start here so their order keys
# (``strategy_id:intent_id``) cannot collide with a strategy's own counter,
# which restarts daily and has reached ~50,000 on THESHOW. Below 2**31.
INTENT_ID_BASE = 1_900_000_000

# An unpriced exit is retried every supervisor tick (1 Hz). Logging each retry
# wrote ``loss_exit_unpriced`` 62,946 times on THESHOW 10/30-10/31 while the
# HALT waited for a book; the counter still counts every tick.
UNPRICED_LOG_INTERVAL_NS = 60 * 1_000_000_000


def is_platform_exit(order_key: str) -> bool:
    """Whether an order key (``strategy_id:intent_id``) names one of these exits."""
    _, _, tail = order_key.rpartition(":")
    return tail.isdigit() and int(tail) >= INTENT_ID_BASE


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
        "_unpriced_logged_ns",
    )

    def __init__(self, *, protection_ticks: int = 5, retry_interval_s: float = 10.0) -> None:
        self._protection_ticks = max(0, int(protection_ticks))
        self._retry_interval_ns = int(retry_interval_s * 1_000_000_000)
        # symbol -> when an exit was last submitted for it.
        self._last_submit_ns: dict[str, int] = {}
        self._next_intent_id = INTENT_ID_BASE
        self._window_ns_cache: dict[tuple[str, int], tuple[int, int]] = {}
        # symbol -> when ``loss_exit_unpriced`` was last logged for it.
        self._unpriced_logged_ns: dict[str, int] = {}

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
        operator_symbols: frozenset[str] = frozenset(),
        kill_switch: bool = False,
    ) -> list[OrderIntent]:
        """Return the exit intents due now.

        ``l1(symbol)`` gives ``(best_bid, best_ask)`` scaled, or None.
        ``position_loss_limit_scaled`` of 0 disables that trigger.
        ``operator_symbols`` are closed because an operator asked.
        ``kill_switch`` closes everything; the caller passes it only with
        verified positions.
        """
        # symbol -> [net_qty, signed qty x entry, {strategy_id: net_qty}, basis_known]
        by_symbol: dict[str, list] = {}
        for pos in positions:
            if pos.net_qty == 0:
                continue
            acc = by_symbol.setdefault(pos.symbol, [0, 0, {}, True])
            acc[0] += pos.net_qty
            acc[1] += pos.net_qty * pos.avg_price_scaled
            # A lot recovered from the broker alone has no entry price (-1); the
            # symbol's loss since entry is then unknown, not zero.
            if pos.avg_price_scaled < 0:
                acc[3] = False
            holders = acc[2]
            sid = pos.strategy_id or UNATTRIBUTED_STRATEGY_ID
            holders[sid] = holders.get(sid, 0) + pos.net_qty

        intents: list[OrderIntent] = []
        due: set[str] = set()
        for symbol, (net_qty, signed_cost, holders, basis_known) in by_symbol.items():
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
                position_loss_limit_scaled=position_loss_limit_scaled if basis_known else 0,
                expiry_enabled=expiry_enabled,
                expiry_cutoff_minute=expiry_cutoff_minute,
                mid=(bid + ask) // 2 if priced else 0,
                multiplier=multiplier,
                operator=symbol in operator_symbols,
                kill_switch=kill_switch,
            )
            if trigger is None:
                continue
            key = symbol
            due.add(key)
            last = self._last_submit_ns.get(key)
            if last is not None and now_ns - last < self._retry_interval_ns:
                continue
            if not priced:
                MetricsRegistry.get().loss_exit_unpriced_total.labels(trigger=trigger).inc()
                logged = self._unpriced_logged_ns.get(key)
                if logged is None or now_ns - logged >= UNPRICED_LOG_INTERVAL_NS:
                    self._unpriced_logged_ns[key] = now_ns
                    logger.warning("loss_exit_unpriced", strategy_id=strategy_id, symbol=symbol, trigger=trigger)
                continue
            self._unpriced_logged_ns.pop(key, None)
            if sum(1 for q in holders.values() if q != 0) > 1:
                logger.warning("loss_exit_shared_symbol", symbol=symbol, holders=dict(holders), booked_to=strategy_id)
            intents.append(self._build(strategy_id, symbol, net_qty, bid, ask, tick_size(symbol), trigger, now_ns))
            self._last_submit_ns[key] = now_ns
            MetricsRegistry.get().loss_exit_orders_total.labels(trigger=trigger).inc()
        # A position that no longer triggers (closed, or recovered) starts fresh next time.
        # (Cancel bookkeeping is ``cancel_intents``' own.)
        for key in [k for k in self._last_submit_ns if k not in due and not k.startswith("cancel:")]:
            del self._last_submit_ns[key]
        for key in [k for k in self._unpriced_logged_ns if k not in due]:
            del self._unpriced_logged_ns[key]
        return intents

    def cancel_intents(
        self, orders: Iterable[tuple[str, str, str]], now_ns: int, *, trigger: str = TRIGGER_OPERATOR
    ) -> list[OrderIntent]:
        """CANCEL intents for resting orders, each retried at most every retry interval.

        ``orders`` are ``(order_key, strategy_id, symbol)``. The platform's own
        exits (intent ids from ``INTENT_ID_BASE``) are left alone: they are IOC
        and are what closes the book.
        """
        intents: list[OrderIntent] = []
        due: set[str] = set()
        for order_key, strategy_id, symbol in orders:
            if is_platform_exit(order_key):
                continue
            key = "cancel:" + order_key
            due.add(key)
            last = self._last_submit_ns.get(key)
            if last is not None and now_ns - last < self._retry_interval_ns:
                continue
            self._next_intent_id += 1
            intents.append(
                OrderIntent(
                    intent_id=self._next_intent_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    intent_type=IntentType.CANCEL,
                    side=Side.BUY,
                    price=0,
                    qty=0,
                    target_order_id=order_key,
                    timestamp_ns=now_ns,
                    source_ts_ns=now_ns,
                    reason=f"loss_exit:{trigger}:cancel",
                )
            )
            self._last_submit_ns[key] = now_ns
        for key in [k for k in self._last_submit_ns if k.startswith("cancel:") and k not in due]:
            del self._last_submit_ns[key]
        if intents:
            logger.warning("loss_exit_cancel_resting", count=len(intents), trigger=trigger)
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
        operator: bool = False,
        kill_switch: bool = False,
    ) -> str | None:
        if daily_halt:
            return TRIGGER_DAILY_LOSS_HALT
        if kill_switch:
            return TRIGGER_KILL_SWITCH
        if operator:
            return TRIGGER_OPERATOR
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
