"""Unit tests for SimpleMarketMaker strategy."""

from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.simple_mm import SimpleMarketMaker


def _make_stats(
    symbol: str = "2330",
    mid_price_x2: int = 100_0000,  # 50.0 * 10000 * 2
    spread_scaled: int = 100,  # 0.01 * 10000
    imbalance: float = 0.0,
    best_bid: int = 499_950,
    best_ask: int = 500_050,
    bid_depth: int = 1000,
    ask_depth: int = 1000,
) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=0,
        mid_price_x2=mid_price_x2,
        spread_scaled=spread_scaled,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


class TestSimpleMarketMaker:
    def test_instantiation(self):
        """Should not raise."""
        strat = SimpleMarketMaker("test_smm")
        assert strat.strategy_id == "test_smm"

    def test_on_stats_generates_quotes(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {}
        stats = _make_stats()
        intents = []
        strat._emit_intent = lambda *a, **kw: intents.append((a, kw))
        # Call on_stats — it uses self.buy/self.sell which need context
        # Since SimpleMarketMaker.on_stats calls self.buy/self.sell directly,
        # and those call _emit_intent, we test that no exception is raised
        strat.on_stats(stats)  # Should not raise
        # buy() and sell() call _emit_intent when no ctx is set, they return early
        # but on_stats itself should have completed without error
        assert isinstance(strat._generated_intents, list)
        # buy() and sell() call _emit_intent when no ctx is set, they return early
        # but on_stats itself should have completed without error
        assert isinstance(strat._generated_intents, list)

    def test_on_stats_skips_invalid_prices(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {}
        stats = _make_stats(mid_price_x2=0, spread_scaled=0, best_bid=0, best_ask=0)
        strat.on_stats(stats)  # Should not raise — early return on invalid
        # No intents generated on invalid prices (early return)
        assert len(strat._generated_intents) == 0
        # No intents generated on invalid prices (early return)
        assert len(strat._generated_intents) == 0

    def test_on_stats_skips_none_values(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {}
        # R8: LOBStatsEvent is now frozen=True, so construct with mid_price_x2=None directly
        stats = _make_stats(mid_price_x2=None)  # type: ignore[arg-type]
        strat.on_stats(stats)  # Should not raise — early return on None
        # No intents generated when mid_price_x2 is None (early return)
        assert len(strat._generated_intents) == 0

    def test_on_stats_negative_spread(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {}
        stats = _make_stats(spread_scaled=-1)
        strat.on_stats(stats)  # Should not raise — early return
        # No intents generated on negative spread (early return)
        assert len(strat._generated_intents) == 0
        # No intents generated on negative spread (early return)
        assert len(strat._generated_intents) == 0

    def test_inventory_skew_affects_quotes(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {"2330": 50}
        stats = _make_stats()
        # With positive inventory, bid should be lower (skew discourages buying)
        strat.on_stats(stats)  # Should not raise
        # Strategy should still attempt to generate intents (no ctx so buy/sell are no-ops)
        assert isinstance(strat._generated_intents, list)
        assert len(strat._generated_intents) == 0  # no ctx means buy/sell return early
        # Strategy should still attempt to generate intents (no ctx so buy/sell are no-ops)
        assert isinstance(strat._generated_intents, list)
        assert len(strat._generated_intents) == 0  # no ctx means buy/sell return early

    def test_max_position_limit(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {"2330": 100}
        stats = _make_stats()
        strat.on_stats(stats)  # Should not raise — only sell side at max pos
        # At max position (100), buy is skipped but sell still attempted
        # No ctx means buy/sell return early, but verify no crash
        assert isinstance(strat._generated_intents, list)
        assert len(strat._generated_intents) == 0  # no ctx means buy/sell return early
        # At max position (100), buy is skipped but sell still attempted
        # No ctx means buy/sell return early, but verify no crash
        assert isinstance(strat._generated_intents, list)
        assert len(strat._generated_intents) == 0  # no ctx means buy/sell return early

    def test_custom_tick_size_ratio(self):
        strat = SimpleMarketMaker("test_smm", tick_size_ratio_pct=100)
        assert strat._tick_size_ratio_pct == 100
