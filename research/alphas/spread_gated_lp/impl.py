"""
Spread-Gated Selective LP (SG-LP) — Research Prototype

Strategy: Only provide liquidity when spread exceeds a profitability threshold.
Optional OBI-momentum skew: single-sided quoting in OBI-favored direction.

TMFD6 Economics:
    1 point = 10 NTD, RT cost = 40 NTD = 4 points
    Breakeven spread: > 4 points
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple


class Side(Enum):
    BUY = 1
    SELL = -1


class FillResult(NamedTuple):
    fill_ts: int       # nanosecond timestamp of fill
    side: Side         # BUY or SELL
    fill_px: float     # fill price (index points)
    spread_at_fill: float  # spread when order was posted
    queue_pos_at_post: float  # estimated queue position when posted
    obi_at_post: float  # OBI when posted


@dataclass(slots=True)
class PendingOrder:
    """A limit order sitting in the book."""
    side: Side
    price: float        # limit price (index points)
    post_ts: int        # timestamp when posted
    queue_ahead: float  # estimated lots ahead of us
    spread_at_post: float
    obi_at_post: float


@dataclass(slots=True)
class SGLPState:
    """Mutable strategy state."""
    position: int = 0  # current position in lots (-1, 0, +1)
    pending_bid: PendingOrder | None = None
    pending_ask: PendingOrder | None = None
    fills: list = field(default_factory=list)
    total_pnl_pts: float = 0.0
    n_cancels: int = 0


class SGLPStrategy:
    """Spread-Gated Selective Liquidity Provider.

    Parameters
    ----------
    spread_gate_pts : int
        Minimum spread to activate quoting (default: 5).
    obi_threshold : float
        OBI threshold for single-sided mode. 0 = always two-sided.
        0.2 = quote only OBI-favored side when |OBI| > 0.2.
    max_position : int
        Maximum absolute position (default: 1).
    fee_per_leg_pts : float
        Fee per leg in points (default: 2.0 = 20 NTD / 10 NTD per pt).
    """

    def __init__(
        self,
        spread_gate_pts: int = 5,
        obi_threshold: float = 0.0,
        max_position: int = 1,
        fee_per_leg_pts: float = 2.0,
    ) -> None:
        self.spread_gate_pts = spread_gate_pts
        self.obi_threshold = obi_threshold
        self.max_position = max_position
        self.fee_per_leg_pts = fee_per_leg_pts
        self.state = SGLPState()

    def reset(self) -> None:
        self.state = SGLPState()

    def compute_obi(self, bid_qty: float, ask_qty: float) -> float:
        """Order book imbalance: (bid - ask) / (bid + ask). Range [-1, +1]."""
        total = bid_qty + ask_qty
        if total <= 0:
            return 0.0
        return (bid_qty - ask_qty) / total

    def should_quote_bid(self, obi: float) -> bool:
        """Determine if we should post a bid (buy) order."""
        if self.state.position >= self.max_position:
            return False
        if self.obi_threshold <= 0:
            return True  # two-sided mode
        # OBI negative (ask heavy) → price likely DOWN → post bid (buy cheap)
        if obi < -self.obi_threshold:
            return True
        return False

    def should_quote_ask(self, obi: float) -> bool:
        """Determine if we should post an ask (sell) order."""
        if self.state.position <= -self.max_position:
            return False
        if self.obi_threshold <= 0:
            return True  # two-sided mode
        # OBI positive (bid heavy) → price likely UP → post ask (sell high)
        if obi > self.obi_threshold:
            return True
        return False

    def on_quote(
        self,
        ts: int,
        bid_px: float,
        ask_px: float,
        bid_qty: float,
        ask_qty: float,
    ) -> None:
        """Process a BidAsk quote update.

        Handles: spread gate, order posting, queue tracking, fill detection, cancels.
        """
        spread = ask_px - bid_px
        obi = self.compute_obi(bid_qty, ask_qty)

        # --- Cancel on tighten ---
        if spread < self.spread_gate_pts:
            if self.state.pending_bid is not None:
                self.state.pending_bid = None
                self.state.n_cancels += 1
            if self.state.pending_ask is not None:
                self.state.pending_ask = None
                self.state.n_cancels += 1
            return

        # --- Fill detection via queue depletion ---
        self._check_fills(ts, bid_px, ask_px, bid_qty, ask_qty)

        # --- Post new orders if eligible ---
        if self.state.pending_bid is None and self.should_quote_bid(obi):
            self.state.pending_bid = PendingOrder(
                side=Side.BUY,
                price=bid_px,
                post_ts=ts,
                queue_ahead=bid_qty,  # join at back of queue
                spread_at_post=spread,
                obi_at_post=obi,
            )

        if self.state.pending_ask is None and self.should_quote_ask(obi):
            self.state.pending_ask = PendingOrder(
                side=Side.SELL,
                price=ask_px,
                post_ts=ts,
                queue_ahead=ask_qty,  # join at back of queue
                spread_at_post=spread,
                obi_at_post=obi,
            )

        # --- Cancel stale orders (price moved away) ---
        if self.state.pending_bid is not None:
            if self.state.pending_bid.price != bid_px:
                # Bid moved, our order is no longer at touch
                if self.state.pending_bid.price < bid_px:
                    # Bid improved past us — cancel (we're now behind)
                    self.state.pending_bid = None
                    self.state.n_cancels += 1
                # If bid dropped, our order is now better than touch — keep

        if self.state.pending_ask is not None:
            if self.state.pending_ask.price != ask_px:
                if self.state.pending_ask.price > ask_px:
                    # Ask improved past us — cancel
                    self.state.pending_ask = None
                    self.state.n_cancels += 1

    def _check_fills(
        self,
        ts: int,
        bid_px: float,
        ask_px: float,
        bid_qty: float,
        ask_qty: float,
    ) -> None:
        """Detect fills via queue depletion at our price level."""

        # Bid fill: someone sold into the bid, depleting the queue
        if self.state.pending_bid is not None:
            order = self.state.pending_bid
            if bid_px == order.price:
                # Same price level — track queue depletion
                if bid_qty < order.queue_ahead:
                    # Queue shrank — update our position
                    consumed = order.queue_ahead - bid_qty
                    order.queue_ahead = max(0.0, order.queue_ahead - consumed)
                    if order.queue_ahead <= 0:
                        # We got filled!
                        self._record_fill(ts, order)
                        self.state.pending_bid = None
                        self.state.position += 1
                elif bid_qty > order.queue_ahead:
                    # Queue grew — someone joined behind us, no change to our position
                    pass
            elif bid_px > order.price:
                # Bid improved — our price is now behind the book
                # This could mean we got filled (trade through) or book moved
                # Conservative: assume we got filled if price crossed our level
                self._record_fill(ts, order)
                self.state.pending_bid = None
                self.state.position += 1
            elif bid_px < order.price:
                # Bid dropped below our price — our order is stale, cancel
                self.state.pending_bid = None
                self.state.n_cancels += 1

        # Ask fill: someone bought into the ask, depleting the queue
        if self.state.pending_ask is not None:
            order = self.state.pending_ask
            if ask_px == order.price:
                if ask_qty < order.queue_ahead:
                    consumed = order.queue_ahead - ask_qty
                    order.queue_ahead = max(0.0, order.queue_ahead - consumed)
                    if order.queue_ahead <= 0:
                        self._record_fill(ts, order)
                        self.state.pending_ask = None
                        self.state.position -= 1
                elif ask_qty > order.queue_ahead:
                    pass
            elif ask_px < order.price:
                # Ask improved — trade through
                self._record_fill(ts, order)
                self.state.pending_ask = None
                self.state.position -= 1
            elif ask_px > order.price:
                # Ask moved away — cancel
                self.state.pending_ask = None
                self.state.n_cancels += 1

    def _record_fill(self, ts: int, order: PendingOrder) -> None:
        self.state.fills.append(FillResult(
            fill_ts=ts,
            side=order.side,
            fill_px=order.price,
            spread_at_fill=order.spread_at_post,
            queue_pos_at_post=order.queue_ahead,
            obi_at_post=order.obi_at_post,
        ))
