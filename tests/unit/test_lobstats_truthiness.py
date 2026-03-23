"""Tests for LOBStatsEvent edge cases — zero-price truthiness bug (C-1)."""

from hft_platform.events import LOBStatsEvent


class TestLOBStatsEventZeroPrice:
    """Verify LOBStatsEvent handles best_bid=0 or best_ask=0 correctly."""

    def test_zero_best_bid_computes_mid_price(self):
        """When best_bid=0 and best_ask has a value, mid_price_x2 should still compute."""
        event = LOBStatsEvent(
            symbol="TEST",
            ts=1,
            imbalance=0.0,
            best_bid=0,
            best_ask=1000000,
            bid_depth=10,
            ask_depth=20,
        )
        # mid_price_x2 = best_bid + best_ask = 0 + 1000000 = 1000000
        assert event.mid_price_x2 == 1000000
        # spread_scaled = best_ask - best_bid = 1000000 - 0 = 1000000
        assert event.spread_scaled == 1000000

    def test_zero_best_ask_computes_mid_price(self):
        """When best_ask=0 and best_bid has a value, mid_price_x2 should still compute."""
        event = LOBStatsEvent(
            symbol="TEST",
            ts=1,
            imbalance=0.0,
            best_bid=1000000,
            best_ask=0,
            bid_depth=10,
            ask_depth=20,
        )
        assert event.mid_price_x2 == 1000000
        assert event.spread_scaled == -1000000

    def test_both_zero_computes_zero(self):
        """When both best_bid=0 and best_ask=0, mid_price_x2 and spread_scaled should be 0."""
        event = LOBStatsEvent(
            symbol="TEST",
            ts=1,
            imbalance=0.0,
            best_bid=0,
            best_ask=0,
            bid_depth=0,
            ask_depth=0,
        )
        assert event.mid_price_x2 == 0
        assert event.spread_scaled == 0

    def test_normal_values_unchanged(self):
        """Regression: normal non-zero values still work correctly."""
        event = LOBStatsEvent(
            symbol="2330",
            ts=1,
            imbalance=0.5,
            best_bid=5000000,
            best_ask=5010000,
            bid_depth=100,
            ask_depth=200,
        )
        assert event.mid_price_x2 == 10010000  # 5000000 + 5010000
        assert event.spread_scaled == 10000  # 5010000 - 5000000
        assert event.mid_price == 5005000.0
        assert event.spread == 10000.0
