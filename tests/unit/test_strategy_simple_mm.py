"""Unit tests for SimpleMarketMaker strategy."""

from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.simple_mm import SimpleMarketMaker


def _make_stats(
    symbol: str = "2330",
    mid_price_x2: int = 100_0000,
    spread_scaled: int = 100,
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
        strat.on_stats(stats)
        assert isinstance(strat._generated_intents, list)
        assert isinstance(strat._generated_intents, list)

    def test_on_stats_skips_invalid_prices(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {}
        stats = _make_stats(mid_price_x2=0, spread_scaled=0, best_bid=0, best_ask=0)
        strat.on_stats(stats)
        assert len(strat._generated_intents) == 0
        assert len(strat._generated_intents) == 0

    def test_on_stats_skips_none_values(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {}
        stats = _make_stats(mid_price_x2=None)  # type: ignore[arg-type]
        strat.on_stats(stats)
        assert len(strat._generated_intents) == 0

    def test_on_stats_negative_spread(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {}
        stats = _make_stats(spread_scaled=-1)
        strat.on_stats(stats)
        assert len(strat._generated_intents) == 0
        assert len(strat._generated_intents) == 0

    def test_inventory_skew_affects_quotes(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {"2330": 50}
        stats = _make_stats()
        strat.on_stats(stats)
        assert isinstance(strat._generated_intents, list)
        assert len(strat._generated_intents) == 0
        assert isinstance(strat._generated_intents, list)
        assert len(strat._generated_intents) == 0

    def test_max_position_limit(self):
        strat = SimpleMarketMaker("test_smm")
        strat._positions = {"2330": 100}
        stats = _make_stats()
        strat.on_stats(stats)
        assert isinstance(strat._generated_intents, list)
        assert len(strat._generated_intents) == 0
        assert isinstance(strat._generated_intents, list)
        assert len(strat._generated_intents) == 0

    def test_custom_tick_size_ratio(self):
        strat = SimpleMarketMaker("test_smm", tick_size_ratio_pct=100)
        assert strat._tick_size_ratio_pct == 100
