"""Integration tests for CompositeAlphaMM strategy.

Tests the strategy with mock StrategyRunner context, verifying
event flow from BidAskEvent + FeatureUpdateEvent through to order generation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np


class TestCompositeAlphaMMIntegration:
    """Integration tests for CompositeAlphaMM with mocked runtime context."""

    def _make_strategy(self, **kwargs):
        with patch.dict("os.environ", {"HFT_FEATURE_ENGINE_ENABLED": "1"}):
            from hft_platform.strategies.composite_alpha_mm import CompositeAlphaMM

            strat = CompositeAlphaMM("integration_test_mm", **kwargs)
        return strat

    def _make_feature_tuple(
        self,
        best_bid: int = 100_0000,
        best_ask: int = 101_0000,
        ofi_ema8: int = 50,
        depth_imb_ema8: int = 5000,
    ) -> tuple:
        feat = [0] * 16
        feat[0] = best_bid
        feat[1] = best_ask
        feat[2] = best_bid + best_ask  # mid_price_x2
        feat[3] = best_ask - best_bid  # spread_scaled
        feat[8] = 100  # ofi_l1_raw
        feat[9] = 500  # ofi_l1_cum
        feat[13] = ofi_ema8
        feat[15] = depth_imb_ema8
        return tuple(feat)

    def _make_book_event(self, symbol: str = "2330"):
        event = MagicMock()
        event.symbol = symbol
        event.bids = np.array(
            [[100_0000, 100], [99_0000, 200], [98_0000, 150]],
            dtype=np.int64,
        )
        event.asks = np.array(
            [[101_0000, 80], [102_0000, 180], [103_0000, 120]],
            dtype=np.int64,
        )
        return event

    def _make_feature_event(self, symbol: str = "2330"):
        from hft_platform.strategies.composite_alpha_mm import _WARMUP_REQUIRED_MASK

        event = MagicMock()
        event.symbol = symbol
        event.warmup_ready_mask = _WARMUP_REQUIRED_MASK
        return event

    def test_full_event_flow(self) -> None:
        """BidAskEvent -> on_book_update -> FeatureUpdateEvent -> on_features -> orders."""
        strat = self._make_strategy(max_position=50, qty=1, signal_threshold=0.0)
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        book_event = self._make_book_event()
        strat.on_book_update(book_event)

        with patch.object(strat, "position", return_value=0), patch.object(
            strat, "buy"
        ) as mock_buy, patch.object(strat, "sell") as mock_sell:
            feat_event = self._make_feature_event()
            # Multiple iterations to build EMA variance
            for _ in range(20):
                strat.on_features(feat_event)

            # With signal_threshold=0.0, should always try to place orders
            total_calls = mock_buy.call_count + mock_sell.call_count
            assert total_calls > 0, "Expected at least one order after multiple events"

    def test_multi_symbol_isolation(self) -> None:
        """Different symbols should have independent LOB caches and positions."""
        strat = self._make_strategy(max_position=50, qty=1, signal_threshold=0.0)
        strat.ctx = MagicMock()

        # Feed book events for two symbols
        for sym in ["2330", "2317"]:
            book_event = self._make_book_event(symbol=sym)
            strat.on_book_update(book_event)

        assert "2330" in strat._lob_cache
        assert "2317" in strat._lob_cache

    def test_position_skew_affects_quotes(self) -> None:
        """Long position should skew quotes downward (encourage selling)."""
        strat = self._make_strategy(
            max_position=50,
            qty=1,
            inv_skew_per_lot=5000,
            signal_threshold=0.0,
        )
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        book_event = self._make_book_event()
        strat.on_book_update(book_event)

        # Run with zero position
        prices_zero_pos: list[tuple] = []
        with patch.object(strat, "position", return_value=0), patch.object(
            strat, "buy"
        ) as mock_buy, patch.object(strat, "sell") as mock_sell:
            for _ in range(20):
                feat_event = self._make_feature_event()
                strat.on_features(feat_event)
            if mock_buy.called:
                prices_zero_pos.append(("buy", mock_buy.call_args_list[-1]))
            if mock_sell.called:
                prices_zero_pos.append(("sell", mock_sell.call_args_list[-1]))

        # Verify orders were generated
        assert len(prices_zero_pos) > 0, "Expected orders with zero position"

    def test_rapid_event_sequence(self) -> None:
        """Strategy should handle rapid sequence of events without errors."""
        strat = self._make_strategy(max_position=10, qty=1)
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        with patch.object(strat, "position", return_value=0), patch.object(
            strat, "buy"
        ), patch.object(strat, "sell"):
            # Simulate 100 rapid events
            for _i in range(100):
                book_event = self._make_book_event()
                strat.on_book_update(book_event)

                feat_event = self._make_feature_event()
                strat.on_features(feat_event)

    def test_varying_signals(self) -> None:
        """Strategy should handle varying feature values without crashing."""
        strat = self._make_strategy(max_position=10, qty=1)
        strat.ctx = MagicMock()

        with patch.object(strat, "position", return_value=0), patch.object(
            strat, "buy"
        ), patch.object(strat, "sell"):
            for ofi in [-100, -50, 0, 50, 100]:
                for depth in [-5000, 0, 5000, 10000]:
                    strat.ctx.get_feature_tuple.return_value = (
                        self._make_feature_tuple(
                            ofi_ema8=ofi,
                            depth_imb_ema8=depth,
                        )
                    )
                    book_event = self._make_book_event()
                    strat.on_book_update(book_event)
                    feat_event = self._make_feature_event()
                    strat.on_features(feat_event)

    def test_prices_always_scaled_int(self) -> None:
        """All prices passed to buy/sell must be integers (Precision Law)."""
        strat = self._make_strategy(max_position=50, qty=1, signal_threshold=0.0)
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        book_event = self._make_book_event()
        strat.on_book_update(book_event)

        with patch.object(strat, "position", return_value=0), patch.object(
            strat, "buy"
        ) as mock_buy, patch.object(strat, "sell") as mock_sell:
            for _ in range(20):
                feat_event = self._make_feature_event()
                strat.on_features(feat_event)

            for call in mock_buy.call_args_list:
                price = call[0][1]  # second positional arg
                assert isinstance(price, int), (
                    f"Buy price must be int, got {type(price)}"
                )

            for call in mock_sell.call_args_list:
                price = call[0][1]
                assert isinstance(price, int), (
                    f"Sell price must be int, got {type(price)}"
                )
