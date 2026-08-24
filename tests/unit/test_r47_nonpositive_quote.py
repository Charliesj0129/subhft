"""R47 must not hand the intent factory a quote at or below zero.

Every widening term in ``_generate_quotes`` (``base_width``, the MFG skew, the
QI skew) is denominated in whole TXF points via ``_PRICE_SCALE``. On an
instrument whose whole premium is worth less than the widening -- an illiquid
option quoted 0.1 / 5.0, say -- those terms subtract more than the mid and the
bid comes out negative.

The platform's under-scaled-price guard in ``StrategyRunner._intent_factory``
does catch such a price, but it catches it by raising ``ValueError`` out of
``handle_event``. The runner reads any exception from a strategy as "this
strategy is broken": it logs ``Strategy Exception``, records a circuit-breaker
failure, and asks ``StrategyGovernor.quarantine()``, which has no TTL and
clears only on a manual re-arm. So a single rejected quote could stop the
strategy trading until an operator intervened. The quote must be dropped at the
decision point instead.
"""

from __future__ import annotations

import pytest

from hft_platform.contracts.strategy import IntentType
from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.r47_maker import R47MakerStrategy
from hft_platform.strategy.base import StrategyContext


def _stats(symbol: str, best_bid: int, best_ask: int, imbalance: float = 0.0) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=1,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=1,
        ask_depth=1,
    )


class _CapturingContext(StrategyContext):
    """Records intents instead of raising, so the test can inspect the price."""

    def __init__(self) -> None:
        self.intents: list[tuple] = []

        def factory(**kwargs):
            self.intents.append((kwargs["symbol"], int(kwargs["intent_type"]), int(kwargs["price"])))
            return kwargs

        super().__init__(
            positions={},
            strategy_id="R47",
            intent_factory=lambda *a, **k: factory(**k),
            price_scaler=lambda _symbol, price: int(price),
        )


@pytest.fixture
def strat() -> R47MakerStrategy:
    # spread_threshold_pts=1 so the wide book below clears the spread gate;
    # the production config uses a threshold tuned for TMF, and the point of
    # this test is the arithmetic downstream of the gate, not the gate.
    return R47MakerStrategy(strategy_id="R47", spread_threshold_pts=1, quote_cooldown_ms=0)


class TestNonPositiveQuoteIsBlocked:
    def test_a_book_whose_spread_exceeds_its_mid_produces_no_intent(self, strat: R47MakerStrategy) -> None:
        ctx = _CapturingContext()

        # 0.1 bid / 5.0 ask: the spread (4.9 pts) is wider than the mid
        # (2.55 pts), so the half-spread widening alone eats the whole mid, and
        # the imbalance adjustment carries the bid the rest of the way past
        # zero. On origin/main this reaches _intent_factory as price=-10000.
        strat.handle_event(ctx, _stats("TXO40900U6", 1_000, 50_000, imbalance=-0.5))

        new_intents = [i for i in ctx.intents if i[1] == int(IntentType.NEW)]
        assert new_intents == []
        assert strat._nonpositive_quote_blocked == 1

    def test_a_normal_futures_book_still_quotes_both_sides(self, strat: R47MakerStrategy) -> None:
        """The guard must not silence the instrument the strategy exists for."""
        ctx = _CapturingContext()

        # TMF around 20,000 pts with a 2-pt spread.
        strat.handle_event(ctx, _stats("TMFI6", 200_000_000, 200_020_000))

        prices = [i[2] for i in ctx.intents if i[1] == int(IntentType.NEW)]
        assert len(prices) == 2
        assert all(p > 0 for p in prices)
