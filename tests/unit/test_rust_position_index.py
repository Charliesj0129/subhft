"""Unit tests for Rust position index for strategy runner (Unit 9)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestRustPositionTracker:
    """Test RustPositionTracker.get_positions_by_strategy() if Rust module available."""

    @pytest.fixture
    def rust_tracker(self):
        try:
            from hft_platform.rust_core import RustPositionTracker
        except ImportError:
            pytest.skip("rust_core not available")
        return RustPositionTracker()

    def test_empty_tracker(self, rust_tracker):
        result = rust_tracker.get_positions_by_strategy()
        assert result == {}

    def test_single_position(self, rust_tracker):
        # BUY (side=0) 10 @ 1000
        rust_tracker.update("acc1:strat_a:SYM1", 0, 10, 1000, 0, 0, 100)
        result = rust_tracker.get_positions_by_strategy()
        assert "strat_a" in result
        assert result["strat_a"]["SYM1"] == 10

    def test_multiple_strategies(self, rust_tracker):
        rust_tracker.update("acc1:strat_a:SYM1", 0, 10, 1000, 0, 0, 100)
        rust_tracker.update("acc1:strat_b:SYM2", 1, 5, 2000, 0, 0, 200)
        result = rust_tracker.get_positions_by_strategy()
        assert "strat_a" in result
        assert "strat_b" in result
        assert result["strat_a"]["SYM1"] == 10
        assert result["strat_b"]["SYM2"] == -5

    def test_same_strategy_multiple_symbols(self, rust_tracker):
        rust_tracker.update("acc1:strat_a:SYM1", 0, 10, 1000, 0, 0, 100)
        rust_tracker.update("acc1:strat_a:SYM2", 0, 20, 2000, 0, 0, 200)
        result = rust_tracker.get_positions_by_strategy()
        assert result["strat_a"]["SYM1"] == 10
        assert result["strat_a"]["SYM2"] == 20

    def test_matches_python_implementation(self, rust_tracker):
        """Verify Rust grouping matches what Python _build_positions_by_strategy does."""
        fills = [
            ("acc1:strat_a:SYM1", 0, 10, 1000),
            ("acc1:strat_a:SYM2", 0, 5, 2000),
            ("acc1:strat_b:SYM1", 1, 3, 1500),
        ]
        for key, side, qty, price in fills:
            rust_tracker.update(key, side, qty, price, 0, 0, 100)

        rust_result = rust_tracker.get_positions_by_strategy()

        # Build Python equivalent
        py_result: dict[str, dict[str, int]] = {}
        for key, side, qty, price in fills:
            parts = key.split(":")
            strat_id = parts[1]
            symbol = parts[2]
            net_qty = qty if side == 0 else -qty
            py_result.setdefault(strat_id, {})[symbol] = py_result.get(strat_id, {}).get(symbol, 0) + net_qty

        assert rust_result == py_result


class TestRunnerRustPositionIntegration:
    """Test that StrategyRunner._build_positions_by_strategy uses Rust tracker."""

    def test_uses_rust_tracker_when_available(self):
        """When position_store has _rust_tracker with get_positions_by_strategy, use it."""
        from hft_platform.strategy.runner import StrategyRunner

        mock_bus = MagicMock()
        mock_risk_queue = MagicMock()
        mock_risk_queue.submit_nowait = MagicMock()

        runner = StrategyRunner.__new__(StrategyRunner)
        runner.position_store = MagicMock()

        mock_rust = MagicMock()
        mock_rust.get_positions_by_strategy.return_value = {"strat_a": {"SYM1": 10}}
        runner.position_store._rust_tracker = mock_rust

        # Initialize needed attributes
        runner._position_key_cache = {}

        result = runner._build_positions_by_strategy()
        assert result == {"strat_a": {"SYM1": 10}}
        mock_rust.get_positions_by_strategy.assert_called_once()

    def test_falls_back_to_python_on_error(self):
        """When Rust tracker raises, falls back to Python path."""
        from hft_platform.strategy.runner import StrategyRunner

        runner = StrategyRunner.__new__(StrategyRunner)
        runner.position_store = MagicMock()

        mock_rust = MagicMock()
        mock_rust.get_positions_by_strategy.side_effect = RuntimeError("boom")
        runner.position_store._rust_tracker = mock_rust
        runner.position_store.positions = {"acc:strat_a:SYM1": MagicMock(net_qty=5)}
        runner._position_key_cache = {}

        result = runner._build_positions_by_strategy()
        # Should not raise, should return something from Python path
        assert isinstance(result, dict)

    def test_falls_back_when_no_rust_tracker(self):
        """When no Rust tracker, use Python path."""
        from hft_platform.strategy.runner import StrategyRunner

        runner = StrategyRunner.__new__(StrategyRunner)
        runner.position_store = MagicMock()
        runner.position_store._rust_tracker = None
        runner.position_store.positions = {}
        runner._position_key_cache = {}

        result = runner._build_positions_by_strategy()
        assert result == {}
