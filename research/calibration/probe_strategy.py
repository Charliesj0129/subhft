"""PassiveQuoteProbe: synthetic strategy for calibrating queue models.

Places symmetric passive quotes at best bid / best ask.
The queue model exponent is a market property (how fills happen as a
function of queue position), not a strategy property — so a simple
passive probe strategy is sufficient to calibrate it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeAction:
    """Action output: quote prices (None = cancel/stand-back) + qty."""

    post_bid_price: int | None
    post_ask_price: int | None
    qty: int


class PassiveQuoteProbe:
    """Symmetric passive market-maker probe.

    Places bid at best_bid and ask at best_ask.
    Stops bidding at long max_pos, stops offering at short max_pos.
    Stands back when spread is zero.
    """

    def __init__(self, qty: int = 1, max_pos: int = 3):
        self.qty = qty
        self.max_pos = max_pos

    def on_tick(
        self, bid: int, ask: int, mid: float, position: int,
    ) -> ProbeAction:
        if ask <= bid:
            return ProbeAction(None, None, self.qty)
        post_bid = bid if position < self.max_pos else None
        post_ask = ask if position > -self.max_pos else None
        return ProbeAction(post_bid, post_ask, self.qty)
