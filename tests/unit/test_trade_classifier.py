"""Tests for EMO trade classifier (Ellis, Michaely, O'Hara 2000).

All prices are scaled int x10000 per Precision Law.
"""

from hft_platform.trade_classifier import (
    BUY,
    CONF_AT_QUOTE,
    CONF_INSIDE,
    CONF_TICK_RULE,
    SELL,
    UNKNOWN,
    TradeClassifier,
)

# Typical TXFD6 prices scaled x10000
BID = 200_000_000  # 20000.0000
ASK = 200_010_000  # 20001.0000
MID_PRICE = 200_005_000  # exact midpoint


def _make_classifier(symbol: str = "TXFD6", bid: int = BID, ask: int = ASK) -> TradeClassifier:
    tc = TradeClassifier(enabled=True)
    tc.update_quotes(symbol, bid, ask)
    return tc


class TestAtQuoteClassification:
    def test_classifies_buy_at_ask(self) -> None:
        tc = _make_classifier()
        direction, confidence = tc.classify("TXFD6", ASK)
        assert direction == BUY
        assert confidence == CONF_AT_QUOTE

    def test_classifies_sell_at_bid(self) -> None:
        tc = _make_classifier()
        direction, confidence = tc.classify("TXFD6", BID)
        assert direction == SELL
        assert confidence == CONF_AT_QUOTE

    def test_classifies_buy_above_ask(self) -> None:
        tc = _make_classifier()
        direction, confidence = tc.classify("TXFD6", ASK + 10_000)
        assert direction == BUY
        assert confidence == CONF_AT_QUOTE

    def test_classifies_sell_below_bid(self) -> None:
        tc = _make_classifier()
        direction, confidence = tc.classify("TXFD6", BID - 10_000)
        assert direction == SELL
        assert confidence == CONF_AT_QUOTE


class TestInsideSpreadClassification:
    def test_classifies_buy_inside_spread_above_mid(self) -> None:
        tc = _make_classifier()
        # Price above midpoint but below ask
        price = MID_PRICE + 1_000
        direction, confidence = tc.classify("TXFD6", price)
        assert direction == BUY
        assert confidence == CONF_INSIDE

    def test_classifies_sell_inside_spread_below_mid(self) -> None:
        tc = _make_classifier()
        # Price below midpoint but above bid
        price = MID_PRICE - 1_000
        direction, confidence = tc.classify("TXFD6", price)
        assert direction == SELL
        assert confidence == CONF_INSIDE


class TestTickRuleFallback:
    def test_tick_rule_fallback_at_midpoint(self) -> None:
        tc = _make_classifier()
        # First trade at ask to set prev_direction = BUY
        tc.classify("TXFD6", ASK)
        # Trade at exact midpoint -> tick rule uses prev direction
        direction, confidence = tc.classify("TXFD6", MID_PRICE)
        assert direction == BUY
        assert confidence == CONF_TICK_RULE

    def test_tick_rule_fallback_at_midpoint_sell(self) -> None:
        tc = _make_classifier()
        # Set prev_direction = SELL
        tc.classify("TXFD6", BID)
        direction, confidence = tc.classify("TXFD6", MID_PRICE)
        assert direction == SELL
        assert confidence == CONF_TICK_RULE

    def test_tick_rule_zero_tick_no_prev(self) -> None:
        tc = _make_classifier()
        # No previous direction set, trade at midpoint
        direction, confidence = tc.classify("TXFD6", MID_PRICE)
        assert direction == UNKNOWN
        assert confidence == CONF_TICK_RULE


class TestEdgeCases:
    def test_no_quotes_returns_unknown(self) -> None:
        tc = TradeClassifier(enabled=True)
        direction, confidence = tc.classify("TXFD6", 200_000_000)
        assert direction == UNKNOWN
        assert confidence == 0

    def test_disabled_returns_zero(self) -> None:
        tc = TradeClassifier(enabled=False)
        tc.update_quotes("TXFD6", BID, ASK)
        direction, confidence = tc.classify("TXFD6", ASK)
        assert direction == UNKNOWN
        assert confidence == 0

    def test_zero_spread(self) -> None:
        """When spread=0 (bid==ask), at-quote rule still works."""
        tc = TradeClassifier(enabled=True)
        same_price = 200_000_000
        tc.update_quotes("TXFD6", same_price, same_price)
        # At bid/ask -> SELL (bid check first) or BUY (ask check first)
        # price >= ask is checked first -> BUY
        direction, confidence = tc.classify("TXFD6", same_price)
        assert direction == BUY
        assert confidence == CONF_AT_QUOTE

    def test_only_bid_no_ask(self) -> None:
        """ask=0, bid>0 -> price <= bid -> SELL."""
        tc = TradeClassifier(enabled=True)
        tc.update_quotes("TXFD6", BID, 0)
        direction, confidence = tc.classify("TXFD6", BID)
        assert direction == SELL
        assert confidence == CONF_AT_QUOTE

    def test_only_ask_no_bid(self) -> None:
        """bid=0, ask>0 -> price >= ask -> BUY."""
        tc = TradeClassifier(enabled=True)
        tc.update_quotes("TXFD6", 0, ASK)
        direction, confidence = tc.classify("TXFD6", ASK)
        assert direction == BUY
        assert confidence == CONF_AT_QUOTE


class TestConfidenceLevels:
    def test_confidence_levels(self) -> None:
        tc = _make_classifier()
        # At ask -> 1000
        _, c1 = tc.classify("TXFD6", ASK)
        assert c1 == 1000

        # Inside spread above mid -> 800
        _, c2 = tc.classify("TXFD6", MID_PRICE + 1_000)
        assert c2 == 800

        # Midpoint with prev direction -> 500
        _, c3 = tc.classify("TXFD6", MID_PRICE)
        assert c3 == 500


class TestPerSymbolIsolation:
    def test_per_symbol_isolation(self) -> None:
        tc = TradeClassifier(enabled=True)
        tc.update_quotes("TXFD6", 200_000_000, 200_010_000)
        tc.update_quotes("TMFD6", 100_000_000, 100_050_000)

        # TXFD6 at ask -> BUY
        d1, _ = tc.classify("TXFD6", 200_010_000)
        assert d1 == BUY

        # TMFD6 at bid -> SELL
        d2, _ = tc.classify("TMFD6", 100_000_000)
        assert d2 == SELL

        # Unknown symbol -> UNKNOWN
        d3, c3 = tc.classify("UNKNOWN", 150_000_000)
        assert d3 == UNKNOWN
        assert c3 == 0


class TestScaledIntNoFloat:
    def test_scaled_int_no_float(self) -> None:
        """Verify all inputs and outputs are int, no float operations."""
        tc = _make_classifier()
        # All inputs are int
        bid = BID
        ask = ASK
        price = MID_PRICE + 1_000
        assert isinstance(bid, int)
        assert isinstance(ask, int)
        assert isinstance(price, int)

        direction, confidence = tc.classify("TXFD6", price)
        assert isinstance(direction, int)
        assert isinstance(confidence, int)

    def test_large_prices_no_overflow(self) -> None:
        """Ensure arithmetic works with large scaled prices."""
        tc = TradeClassifier(enabled=True)
        # Very large price: 99999.9999 * 10000 = 999_999_999
        big_bid = 999_990_000
        big_ask = 1_000_010_000
        tc.update_quotes("BIG", big_bid, big_ask)

        direction, confidence = tc.classify("BIG", big_ask)
        assert direction == BUY
        assert confidence == CONF_AT_QUOTE


class TestPrevDirectionPersistence:
    def test_tick_rule_persists_across_trades(self) -> None:
        tc = _make_classifier()
        # BUY at ask
        tc.classify("TXFD6", ASK)
        # Midpoint -> tick rule BUY
        d1, _ = tc.classify("TXFD6", MID_PRICE)
        assert d1 == BUY

        # SELL at bid
        tc.classify("TXFD6", BID)
        # Midpoint -> tick rule SELL
        d2, _ = tc.classify("TXFD6", MID_PRICE)
        assert d2 == SELL

    def test_quote_update_does_not_reset_direction(self) -> None:
        tc = _make_classifier()
        tc.classify("TXFD6", ASK)  # sets prev_direction = BUY
        # Update quotes
        tc.update_quotes("TXFD6", BID + 10_000, ASK + 10_000)
        # New midpoint
        new_mid = (BID + 10_000 + ASK + 10_000) // 2
        d, c = tc.classify("TXFD6", new_mid)
        # prev_direction should still be BUY from before
        assert d == BUY
        assert c == CONF_TICK_RULE


class TestCrossedMarketGuard:
    def test_crossed_market_returns_unknown(self) -> None:
        """When best_bid > best_ask (crossed market), return UNKNOWN."""
        tc = TradeClassifier(enabled=True)
        tc.update_quotes("TXFD6", 200_020_000, 200_000_000)  # bid > ask
        direction, confidence = tc.classify("TXFD6", 200_010_000)
        assert direction == UNKNOWN
        assert confidence == 0

    def test_crossed_market_increments_unknown_counter(self) -> None:
        tc = TradeClassifier(enabled=True)
        tc.update_quotes("TXFD6", 200_020_000, 200_000_000)
        tc.classify("TXFD6", 200_010_000)
        assert tc.count_unknown == 1


class TestClassificationCounters:
    def test_counters_increment_correctly(self) -> None:
        tc = _make_classifier()

        # 2 at-quote classifications
        tc.classify("TXFD6", ASK)
        tc.classify("TXFD6", BID)
        assert tc.count_at_quote == 2

        # 1 inside-spread classification
        tc.classify("TXFD6", MID_PRICE + 1_000)
        assert tc.count_inside == 1

        # 1 tick-rule classification (midpoint, prev_direction set)
        tc.classify("TXFD6", MID_PRICE)
        assert tc.count_tick_rule == 1

        # 1 unknown (no quotes for symbol)
        tc.classify("NOSYMBOL", 100_000)
        assert tc.count_unknown == 1

    def test_get_stats_returns_distribution(self) -> None:
        tc = _make_classifier()
        tc.classify("TXFD6", ASK)  # at_quote
        tc.classify("TXFD6", BID)  # at_quote
        tc.classify("TXFD6", MID_PRICE + 1_000)  # inside
        tc.classify("TXFD6", MID_PRICE)  # tick_rule

        stats = tc.get_stats()
        assert stats["count_at_quote"] == 2
        assert stats["count_inside"] == 1
        assert stats["count_tick_rule"] == 1
        assert stats["count_unknown"] == 0
        assert stats["total"] == 4

    def test_disabled_does_not_increment_counters(self) -> None:
        tc = TradeClassifier(enabled=False)
        tc.update_quotes("TXFD6", BID, ASK)
        tc.classify("TXFD6", ASK)
        stats = tc.get_stats()
        assert stats["total"] == 0
