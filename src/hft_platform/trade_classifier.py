"""EMO trade classification for large-tick futures.

Ellis, Michaely, O'Hara (2000) algorithm adapted for TAIFEX futures.
All arithmetic uses scaled integers (Precision Law).

Algorithm:
- price >= best_ask -> BUY (+1)
- price <= best_bid -> SELL (-1)
- Inside spread: compare price*2 vs (best_bid + best_ask)
- At midpoint: tick rule fallback (use previous trade direction)
"""

from __future__ import annotations

import os

from structlog import get_logger

logger = get_logger("trade_classifier")

_ENABLED_DEFAULT = os.getenv("HFT_TRADE_CLASSIFICATION_ENABLED", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# Direction constants
BUY: int = 1
SELL: int = -1
UNKNOWN: int = 0

# Confidence levels (scaled x1000)
CONF_AT_QUOTE: int = 1000  # Trade at bid or ask
CONF_INSIDE: int = 800  # Trade inside spread (not at midpoint)
CONF_TICK_RULE: int = 500  # Tick rule fallback


class _SymbolState:
    """Per-symbol classification state. Pre-allocated, zero-copy."""

    __slots__ = ("last_bid", "last_ask", "prev_direction")

    def __init__(self) -> None:
        self.last_bid: int = 0
        self.last_ask: int = 0
        self.prev_direction: int = 0


class TradeClassifier:
    """EMO trade classification for large-tick futures.

    Algorithm:
    - price >= best_ask -> BUY (+1)
    - price <= best_bid -> SELL (-1)
    - Inside spread: compare price*2 vs (best_bid + best_ask)
    - At midpoint: tick rule fallback
    """

    __slots__ = (
        "_states",
        "_enabled",
        "count_at_quote",
        "count_inside",
        "count_tick_rule",
        "count_unknown",
    )

    def __init__(self, *, enabled: bool | None = None) -> None:
        self._states: dict[str, _SymbolState] = {}
        self._enabled: bool = enabled if enabled is not None else _ENABLED_DEFAULT
        self.count_at_quote: int = 0
        self.count_inside: int = 0
        self.count_tick_rule: int = 0
        self.count_unknown: int = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def update_quotes(self, symbol: str, best_bid: int, best_ask: int) -> None:
        """Cache latest best bid/ask for a symbol. Called on BidAskEvent."""
        state = self._states.get(symbol)
        if state is None:
            state = _SymbolState()
            self._states[symbol] = state
        state.last_bid = best_bid
        state.last_ask = best_ask

    def get_stats(self) -> dict[str, int]:
        """Return classification distribution counters (cold-path only)."""
        total = self.count_at_quote + self.count_inside + self.count_tick_rule + self.count_unknown
        return {
            "count_at_quote": self.count_at_quote,
            "count_inside": self.count_inside,
            "count_tick_rule": self.count_tick_rule,
            "count_unknown": self.count_unknown,
            "total": total,
        }

    def classify(self, symbol: str, price: int) -> tuple[int, int]:
        """Classify a trade as BUY/SELL/UNKNOWN.

        Args:
            symbol: Instrument symbol.
            price: Trade price (scaled int x10000).

        Returns:
            (direction, confidence) where direction is +1/-1/0
            and confidence is scaled x1000 (1000/800/500/0).
        """
        if not self._enabled:
            return (UNKNOWN, 0)

        state = self._states.get(symbol)
        if state is None or (state.last_bid == 0 and state.last_ask == 0):
            self.count_unknown += 1
            return (UNKNOWN, 0)

        best_bid = state.last_bid
        best_ask = state.last_ask

        # Crossed market guard: bid > ask during fast markets/settlement
        # Only trigger when both sides are present (> 0)
        if best_bid > 0 and best_ask > 0 and best_bid > best_ask:
            self.count_unknown += 1
            return (UNKNOWN, 0)

        # At or above ask -> BUY
        if price >= best_ask and best_ask > 0:
            state.prev_direction = BUY
            self.count_at_quote += 1
            return (BUY, CONF_AT_QUOTE)

        # At or below bid -> SELL
        if price <= best_bid and best_bid > 0:
            state.prev_direction = SELL
            self.count_at_quote += 1
            return (SELL, CONF_AT_QUOTE)

        # Inside spread: compare 2*price vs (bid + ask) to avoid division
        mid_x2 = best_bid + best_ask
        trade_x2 = price * 2

        if trade_x2 > mid_x2:
            state.prev_direction = BUY
            self.count_inside += 1
            return (BUY, CONF_INSIDE)

        if trade_x2 < mid_x2:
            state.prev_direction = SELL
            self.count_inside += 1
            return (SELL, CONF_INSIDE)

        # At midpoint: tick rule fallback
        self.count_tick_rule += 1
        direction = state.prev_direction
        if direction != UNKNOWN:
            return (direction, CONF_TICK_RULE)

        return (UNKNOWN, CONF_TICK_RULE)
