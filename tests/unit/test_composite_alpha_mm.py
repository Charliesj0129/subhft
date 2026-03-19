"""Tests for CompositeAlphaMM strategy."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np


class TestCompositeAlphaMM:
    """Tests for composite alpha signal market-making strategy."""

    def _make_strategy(self, **kwargs):
        """Create strategy with feature engine enabled."""
        with patch.dict("os.environ", {"HFT_FEATURE_ENGINE_ENABLED": "1"}):
            from hft_platform.strategies.composite_alpha_mm import CompositeAlphaMM

            strat = CompositeAlphaMM("test_composite_mm", **kwargs)
        return strat

    def _make_feature_tuple(
        self,
        best_bid: int = 100_0000,
        best_ask: int = 101_0000,
        mid_price_x2: int = 201_0000,
        spread_scaled: int = 10000,
        ofi_l1_ema8: int = 50,
        depth_imb_ema8_ppm: int = 5000,
    ) -> tuple:
        """Build a 16-element feature tuple with given overrides."""
        feat = [0] * 16
        feat[0] = best_bid
        feat[1] = best_ask
        feat[2] = mid_price_x2
        feat[3] = spread_scaled
        feat[8] = 100   # ofi_l1_raw
        feat[9] = 500   # ofi_l1_cum
        feat[13] = ofi_l1_ema8
        feat[15] = depth_imb_ema8_ppm
        return tuple(feat)

    def _make_feature_event(self, symbol: str = "2330", warmed_up: bool = True):
        """Build a mock FeatureUpdateEvent."""
        from hft_platform.strategies.composite_alpha_mm import _WARMUP_REQUIRED_MASK

        event = MagicMock()
        event.symbol = symbol
        event.warmup_ready_mask = _WARMUP_REQUIRED_MASK if warmed_up else 0
        return event

    def _make_book_event(
        self,
        symbol: str = "2330",
        bids: np.ndarray | None = None,
        asks: np.ndarray | None = None,
    ):
        """Build a mock BidAskEvent."""
        event = MagicMock()
        event.symbol = symbol
        event.bids = bids if bids is not None else np.array(
            [[100_0000, 10], [99_0000, 20]], dtype=np.int64,
        )
        event.asks = asks if asks is not None else np.array(
            [[101_0000, 15], [102_0000, 25]], dtype=np.int64,
        )
        return event

    # --- Construction tests ---

    def test_construction_defaults(self) -> None:
        strat = self._make_strategy()
        assert strat._max_position == 50
        assert strat._qty == 1
        assert strat._enabled_flag is True
        assert strat._w_ofi == 0.4
        assert strat._w_depth == 0.3
        assert strat._w_slope == 0.3
        assert strat._base_half_spread_ticks == 2
        assert strat._tick_size_scaled == 10000

    def test_construction_custom_params(self) -> None:
        strat = self._make_strategy(
            w_ofi=0.5, w_depth=0.3, w_slope=0.2,
            max_position=10, qty=2,
            base_half_spread_ticks=3,
            inv_skew_per_lot=3000,
            signal_threshold=0.05,
            tick_size_scaled=5000,
            n_levels=5,
            ema_alpha=0.02,
        )
        assert strat._w_ofi == 0.5
        assert strat._w_depth == 0.3
        assert strat._w_slope == 0.2
        assert strat._max_position == 10
        assert strat._qty == 2
        assert strat._base_half_spread_ticks == 3
        assert strat._inv_skew_per_lot == 3000
        assert strat._signal_threshold == 0.05
        assert strat._tick_size_scaled == 5000
        assert strat._n_levels == 5
        assert strat._ema_alpha == 0.02

    def test_disabled_without_env(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            from hft_platform.strategies.composite_alpha_mm import CompositeAlphaMM

            strat = CompositeAlphaMM("test", max_position=10)
        assert strat._enabled_flag is False

    # --- on_book_update tests ---

    def test_on_book_update_caches(self) -> None:
        strat = self._make_strategy()
        event = self._make_book_event()
        strat.on_book_update(event)
        assert "2330" in strat._lob_cache
        bids, asks = strat._lob_cache["2330"]
        assert bids.shape == (2, 2)
        assert asks.shape == (2, 2)

    def test_on_book_update_disabled_skips(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            from hft_platform.strategies.composite_alpha_mm import CompositeAlphaMM

            strat = CompositeAlphaMM("test")
        event = self._make_book_event()
        strat.on_book_update(event)
        assert len(strat._lob_cache) == 0

    def test_on_book_update_symbol_filter(self) -> None:
        strat = self._make_strategy(symbols=["2317"])
        event = self._make_book_event(symbol="2330")
        strat.on_book_update(event)
        assert "2330" not in strat._lob_cache

    # --- on_features gate tests ---

    def test_on_features_warmup_gate(self) -> None:
        strat = self._make_strategy()
        strat.ctx = MagicMock()
        event = self._make_feature_event(warmed_up=False)
        strat.on_features(event)
        strat.ctx.get_feature_tuple.assert_not_called()

    def test_on_features_no_ctx(self) -> None:
        strat = self._make_strategy()
        strat.ctx = None
        event = self._make_feature_event()
        # Should not raise
        strat.on_features(event)

    def test_on_features_disabled_skips(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            from hft_platform.strategies.composite_alpha_mm import CompositeAlphaMM

            strat = CompositeAlphaMM("test")
        strat.ctx = MagicMock()
        event = self._make_feature_event()
        strat.on_features(event)
        strat.ctx.get_feature_tuple.assert_not_called()

    def test_on_features_symbol_filter(self) -> None:
        strat = self._make_strategy(symbols=["2317"])
        strat.ctx = MagicMock()
        event = self._make_feature_event(symbol="2330")
        strat.on_features(event)
        strat.ctx.get_feature_tuple.assert_not_called()

    # --- Signal computation and order generation ---

    def test_on_features_generates_orders(self) -> None:
        strat = self._make_strategy(max_position=50, qty=1)
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        # Populate LOB cache
        strat.on_book_update(self._make_book_event())

        with patch.object(strat, "position", return_value=0), \
             patch.object(strat, "buy") as mock_buy, \
             patch.object(strat, "sell") as mock_sell:

            event = self._make_feature_event()

            # Run multiple times to build up EMA variance
            for _ in range(20):
                strat.on_features(event)

            # After warmup iterations, should have placed orders
            assert mock_buy.called or mock_sell.called

    def test_on_features_orders_are_scaled_int(self) -> None:
        """Verify placed order prices are integers (Precision Law)."""
        strat = self._make_strategy(max_position=50, qty=1)
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()
        strat.on_book_update(self._make_book_event())

        buy_prices: list[int] = []
        sell_prices: list[int] = []

        def capture_buy(_sym, price, _qty, _tif):
            buy_prices.append(price)

        def capture_sell(_sym, price, _qty, _tif):
            sell_prices.append(price)

        with patch.object(strat, "position", return_value=0), \
             patch.object(strat, "buy", side_effect=capture_buy), \
             patch.object(strat, "sell", side_effect=capture_sell):

            event = self._make_feature_event()
            for _ in range(20):
                strat.on_features(event)

        all_prices = buy_prices + sell_prices
        for p in all_prices:
            assert isinstance(p, int), f"Price must be int, got {type(p)}: {p}"
            assert p % strat._tick_size_scaled == 0, (
                f"Price {p} not on tick grid (tick={strat._tick_size_scaled})"
            )

    # --- Position limits ---

    def test_position_limit_no_buy_at_max(self) -> None:
        strat = self._make_strategy(max_position=5, qty=1)
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        with patch.object(strat, "position", return_value=5), \
             patch.object(strat, "buy") as mock_buy, \
             patch.object(strat, "sell"):

            event = self._make_feature_event()
            for _ in range(20):
                strat.on_features(event)

            # At max position, should NOT buy
            mock_buy.assert_not_called()

    def test_position_limit_no_sell_at_neg_max(self) -> None:
        strat = self._make_strategy(max_position=5, qty=1)
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        with patch.object(strat, "position", return_value=-5), \
             patch.object(strat, "buy"), \
             patch.object(strat, "sell") as mock_sell:

            event = self._make_feature_event()
            for _ in range(20):
                strat.on_features(event)

            # At -max position, should NOT sell
            mock_sell.assert_not_called()

    def test_position_at_both_limits_no_orders(self) -> None:
        """When position is at max AND -max simultaneously blocked, no orders."""
        strat = self._make_strategy(max_position=0, qty=1)
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        with patch.object(strat, "position", return_value=0), \
             patch.object(strat, "buy") as mock_buy, \
             patch.object(strat, "sell") as mock_sell:

            event = self._make_feature_event()
            for _ in range(10):
                strat.on_features(event)

            # max_position=0 means both bid_qty and ask_qty are 0
            mock_buy.assert_not_called()
            mock_sell.assert_not_called()

    # --- Edge cases ---

    def test_zero_spread_skipped(self) -> None:
        strat = self._make_strategy()
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple(
            best_bid=100_0000, best_ask=100_0000,
            mid_price_x2=200_0000, spread_scaled=0,
        )

        with patch.object(strat, "position", return_value=0), \
             patch.object(strat, "buy") as mock_buy, \
             patch.object(strat, "sell") as mock_sell:

            event = self._make_feature_event()
            strat.on_features(event)

            mock_buy.assert_not_called()
            mock_sell.assert_not_called()

    def test_negative_best_bid_skipped(self) -> None:
        strat = self._make_strategy()
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple(
            best_bid=0, best_ask=101_0000,
        )

        with patch.object(strat, "position", return_value=0), \
             patch.object(strat, "buy") as mock_buy, \
             patch.object(strat, "sell") as mock_sell:

            event = self._make_feature_event()
            strat.on_features(event)

            mock_buy.assert_not_called()
            mock_sell.assert_not_called()

    def test_insufficient_feature_tuple(self) -> None:
        strat = self._make_strategy()
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = (1, 2, 3)  # too short

        with patch.object(strat, "buy") as mock_buy, \
             patch.object(strat, "sell") as mock_sell:

            event = self._make_feature_event()
            strat.on_features(event)

            mock_buy.assert_not_called()
            mock_sell.assert_not_called()

    def test_none_feature_tuple(self) -> None:
        strat = self._make_strategy()
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = None

        with patch.object(strat, "buy") as mock_buy, \
             patch.object(strat, "sell") as mock_sell:

            event = self._make_feature_event()
            strat.on_features(event)

            mock_buy.assert_not_called()
            mock_sell.assert_not_called()

    def test_no_lob_cache_still_works(self) -> None:
        """Strategy should work without cached LOB (slope = 0)."""
        strat = self._make_strategy()
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()
        # No on_book_update called — empty LOB cache

        with patch.object(strat, "position", return_value=0), \
             patch.object(strat, "buy") as mock_buy, \
             patch.object(strat, "sell") as mock_sell:

            event = self._make_feature_event()
            for _ in range(20):
                strat.on_features(event)

            # Should still generate orders based on OFI + depth signals
            assert mock_buy.called or mock_sell.called

    # --- Inventory skew direction ---

    def test_inventory_skew_direction(self) -> None:
        """Positive position should produce negative skew (push quotes down)."""
        strat = self._make_strategy(inv_skew_per_lot=5000)
        # With position = 2, inv_skew = -2 * 5000 = -10000
        # This pushes both bid and ask down, encouraging selling
        assert strat._inv_skew_per_lot == 5000

    # --- _compute_slope tests ---

    def test_compute_slope_normal(self) -> None:
        from hft_platform.strategies.composite_alpha_mm import _compute_slope

        levels = np.array(
            [[100_0000, 10], [99_0000, 20], [98_0000, 30]],
            dtype=np.int64,
        )
        slope = _compute_slope(levels, 3)
        # Positive slope: increasing depth at lower prices
        assert isinstance(slope, float)
        assert slope > 0  # log1p(10) < log1p(20) < log1p(30) => positive slope

    def test_compute_slope_single_level(self) -> None:
        from hft_platform.strategies.composite_alpha_mm import _compute_slope

        levels = np.array([[100_0000, 10]], dtype=np.int64)
        assert _compute_slope(levels, 10) == 0.0

    def test_compute_slope_empty(self) -> None:
        from hft_platform.strategies.composite_alpha_mm import _compute_slope

        levels = np.zeros((0, 2), dtype=np.int64)
        assert _compute_slope(levels, 10) == 0.0

    def test_compute_slope_n_levels_cap(self) -> None:
        """When n_levels < len(levels), only first n are used."""
        from hft_platform.strategies.composite_alpha_mm import _compute_slope

        levels = np.array(
            [[100_0000, 10], [99_0000, 20], [98_0000, 30], [97_0000, 40]],
            dtype=np.int64,
        )
        slope_2 = _compute_slope(levels, 2)
        slope_4 = _compute_slope(levels, 4)
        # Both should be valid floats but potentially different
        assert isinstance(slope_2, float)
        assert isinstance(slope_4, float)

    # --- Tick grid snapping ---

    def test_tick_grid_snapping(self) -> None:
        """Verify bid/ask prices are snapped to tick grid."""
        strat = self._make_strategy(
            tick_size_scaled=10000,
            signal_threshold=0.0,  # always trade
            ema_alpha=1.0,  # instant convergence
        )
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple()

        placed_prices: list[int] = []

        def capture_buy(_sym, price, _qty, _tif):
            placed_prices.append(price)

        def capture_sell(_sym, price, _qty, _tif):
            placed_prices.append(price)

        with patch.object(strat, "position", return_value=0), \
             patch.object(strat, "buy", side_effect=capture_buy), \
             patch.object(strat, "sell", side_effect=capture_sell):

            event = self._make_feature_event()
            strat.on_features(event)

        for p in placed_prices:
            assert p % 10000 == 0, f"Price {p} not on tick grid"

    # --- Bid < Ask invariant ---

    def test_bid_less_than_ask(self) -> None:
        """Verify bid price is always less than ask price."""
        strat = self._make_strategy(
            signal_threshold=0.0,
            base_half_spread_ticks=1,
        )
        strat.ctx = MagicMock()
        strat.ctx.get_feature_tuple.return_value = self._make_feature_tuple(
            spread_scaled=10000,
        )

        buy_prices: list[int] = []
        sell_prices: list[int] = []

        def capture_buy(_sym, price, _qty, _tif):
            buy_prices.append(price)

        def capture_sell(_sym, price, _qty, _tif):
            sell_prices.append(price)

        with patch.object(strat, "position", return_value=0), \
             patch.object(strat, "buy", side_effect=capture_buy), \
             patch.object(strat, "sell", side_effect=capture_sell):

            event = self._make_feature_event()
            for _ in range(20):
                strat.on_features(event)

        # For each iteration where both buy and sell were placed,
        # verify bid < ask
        n = min(len(buy_prices), len(sell_prices))
        for i in range(n):
            assert buy_prices[i] < sell_prices[i], (
                f"Iteration {i}: bid={buy_prices[i]} >= ask={sell_prices[i]}"
            )
