"""An unresolved contract family must not read as a wildcard.

On 2026-08-23, ``config/live/strategies.yaml`` dropped the expired seed symbol
``TMFE6`` from ``R47_MAKER_TMF`` and left the ``TMF:R1`` family to bind the
front month. That was correct, but it exposed a fail-open reading in
``BaseStrategy.handle_event``: the symbol filter was ``if ... and self.symbols``,
so an empty set skipped the filter entirely. In the 2.4 s between process start
and the first family rebind, the strategy therefore received market data for
every subscribed instrument on the bus and quoted ``TXO40900U6`` at ``-20000``.
"""

from __future__ import annotations

from datetime import date

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.ref import ContractFamily, FamilyCode, FutureRef, Product
from hft_platform.contracts.strategy import Side
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy, StrategyContext


def _stats(symbol: str, best_bid: int, best_ask: int) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=1,
        imbalance=0.0,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=10,
        ask_depth=10,
    )


class _Recorder(BaseStrategy):
    """Records every event that survives the dispatch filter."""

    def __init__(self, strategy_id: str = "REC", **kwargs) -> None:
        super().__init__(strategy_id, **kwargs)
        self.seen: list[str] = []

    def on_stats(self, event: LOBStatsEvent) -> None:  # noqa: D102
        self.seen.append(event.symbol)

    def on_fill(self, event: FillEvent) -> None:  # noqa: D102
        self.seen.append(event.symbol)


TMF_R1 = ContractFamily(product=Product.FUTURE, root="TMF", family=FamilyCode.R1)


@pytest.fixture
def ctx() -> StrategyContext:
    def _no_intents(*args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("no intent should be produced by these tests")

    return StrategyContext(
        positions={},
        strategy_id="REC",
        intent_factory=_no_intents,
        price_scaler=lambda _symbol, price: int(price),
    )


class TestUnboundFamilyIsNotAWildcard:
    def test_market_data_is_dropped_while_a_declared_family_is_unbound(self, ctx: StrategyContext) -> None:
        strat = _Recorder()
        strat.contract_families = (TMF_R1,)
        strat.symbols = set()

        strat.handle_event(ctx, _stats("TXO40900U6", 1000, 3000))

        assert strat.seen == []
        assert strat._unbound_dispatch_skipped == 1

    def test_market_data_flows_again_once_the_family_binds(self, ctx: StrategyContext) -> None:
        strat = _Recorder()
        strat.contract_families = (TMF_R1,)
        strat.symbols = set()
        strat.handle_event(ctx, _stats("TMFI6", 1_000_000, 1_000_100))
        assert strat.seen == []

        # What StrategyRunner._on_family_rebind does when the resolver lands.
        strat.symbols = {FutureRef("TMF", date(2026, 9, 16), FamilyCode.R1).display()}

        strat.handle_event(ctx, _stats("TMFI6", 1_000_000, 1_000_100))
        strat.handle_event(ctx, _stats("TXO40900U6", 1000, 3000))

        assert strat.seen == ["TMFI6"]

    def test_a_strategy_that_declares_no_scope_at_all_still_sees_everything(self, ctx: StrategyContext) -> None:
        """The wildcard is the documented behaviour for an unscoped strategy.

        Only a strategy that declared a family it has not bound yet fails
        closed; narrowing the empty-set case globally would silence every
        strategy that intentionally subscribes to the whole bus.
        """
        strat = _Recorder()
        strat.symbols = set()

        strat.handle_event(ctx, _stats("TXO40900U6", 1000, 3000))

        assert strat.seen == ["TXO40900U6"]
        assert strat._unbound_dispatch_skipped == 0

    def test_an_explicit_symbol_list_still_filters_without_any_family(self, ctx: StrategyContext) -> None:
        strat = _Recorder()
        strat.symbols = {"TMFI6"}

        strat.handle_event(ctx, _stats("TXO40900U6", 1000, 3000))
        strat.handle_event(ctx, _stats("TMFI6", 1_000_000, 1_000_100))

        assert strat.seen == ["TMFI6"]

    def test_fills_are_never_dropped_by_the_unbound_guard(self, ctx: StrategyContext) -> None:
        """A fill on a rolled-off month must still reach the strategy.

        The strategy owns that position; dropping the fill would leave its
        local position permanently wrong.
        """
        strat = _Recorder()
        strat.contract_families = (TMF_R1,)
        strat.symbols = set()

        fill = FillEvent(
            fill_id="F1",
            account_id="ACC",
            order_id="O1",
            strategy_id="REC",
            symbol="TMFE6",
            side=Side.BUY,
            qty=1,
            price=1_000_000,
            fee=0,
            tax=0,
            ingest_ts_ns=1,
            match_ts_ns=1,
        )
        strat.handle_event(ctx, fill)

        assert strat.seen == ["TMFE6"]
        assert strat._unbound_dispatch_skipped == 0
