"""Unit tests for strategy dispatch HashMap index (Unit 10)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.strategy.runner import StrategyRunner


class TestStrategyDispatchIndex:
    def _make_runner(self, strategies=None):
        """Create a StrategyRunner with minimal mocking."""
        runner = StrategyRunner.__new__(StrategyRunner)
        runner.bus = MagicMock()
        runner.risk_queue = MagicMock()
        runner.risk_queue.submit_nowait = MagicMock()
        runner.lob_engine = None
        runner.feature_engine = None
        runner.position_store = None
        runner.strategies = []
        runner._strat_executors = []
        runner._strat_index = {}
        runner._risk_submit = runner.risk_queue.submit_nowait
        runner._risk_submit_typed = None
        runner._typed_intent_fastpath = False
        runner._lob_snapshot_source = None
        runner._lob_l1_source = None
        runner._feature_value_source = None
        runner._feature_view_source = None
        runner._feature_set_source = None
        runner._feature_profile_source = None
        runner._feature_tuple_source = None
        runner.metrics = None
        runner.latency = None
        runner._trace_sampler = None
        runner._obs_policy = ""
        runner._diagnostic_metrics_enabled = False
        runner.symbol_metadata = MagicMock()
        runner.price_codec = MagicMock()
        runner._intent_seq = 0
        runner._positions_cache = {}
        runner._positions_dirty = True
        runner._current_source_ts_ns = 0
        runner._current_trace_id = ""
        runner._strategy_metrics_sample_every = 1
        runner._strategy_metrics_batch = 1
        runner._strategy_metrics_seq = {}
        runner._strategy_pending_intents = {}
        runner._strategy_pending_alpha_intent = {}
        runner._strategy_pending_alpha_flat = {}
        runner._circuit_threshold = 10
        runner._circuit_recovery_threshold = 5
        runner._circuit_cooldown_ns = 60_000_000_000
        runner._failure_counts = {}
        runner._circuit_states = {}
        runner._circuit_success_counts = {}
        runner._circuit_halted_at_ns = {}
        runner._rust_circuit = None
        runner._position_key_cache = {}
        runner._feature_compat_fail_fast = False
        runner.running = False
        runner.registry = MagicMock()
        runner.registry.instantiate.return_value = []

        if strategies:
            for s in strategies:
                runner.strategies.append(s)
                runner._strat_executors.append((s, MagicMock(), None, None, None, None, None))

            runner._strat_index = {}
            for idx, s in enumerate(runner.strategies):
                runner._strat_index.setdefault(s.strategy_id, []).append(idx)

        return runner

    def _make_strategy(self, strategy_id: str, enabled: bool = True):
        s = MagicMock()
        s.strategy_id = strategy_id
        s.enabled = enabled
        s.symbols = set()
        s.handle_event.return_value = []
        return s

    def test_index_built_correctly(self):
        s1 = self._make_strategy("strat_a")
        s2 = self._make_strategy("strat_b")
        s3 = self._make_strategy("strat_a")  # duplicate ID
        runner = self._make_runner([s1, s2, s3])

        assert runner._strat_index["strat_a"] == [0, 2]
        assert runner._strat_index["strat_b"] == [1]

    def test_rebuild_executors_rebuilds_index(self):
        s1 = self._make_strategy("strat_a")
        s2 = self._make_strategy("strat_b")
        runner = self._make_runner([s1, s2])

        # Manually clear and rebuild
        runner._strat_index = {}
        runner._rebuild_executors()

        assert "strat_a" in runner._strat_index
        assert "strat_b" in runner._strat_index

    @pytest.mark.asyncio
    async def test_targeted_dispatch_uses_index(self):
        s1 = self._make_strategy("strat_a")
        s2 = self._make_strategy("strat_b")
        runner = self._make_runner([s1, s2])

        event = MagicMock()
        event.strategy_id = "strat_b"
        event.symbol = "SYM1"
        event.meta = None
        event.ts = 1000

        await runner.process_event(event)

        # strat_b should be called, strat_a should NOT
        s2.handle_event.assert_called_once()
        s1.handle_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_dispatch_calls_all(self):
        s1 = self._make_strategy("strat_a")
        s2 = self._make_strategy("strat_b")
        runner = self._make_runner([s1, s2])

        event = MagicMock()
        event.strategy_id = None  # broadcast
        event.symbol = "SYM1"
        event.meta = None
        event.ts = 1000

        await runner.process_event(event)

        s1.handle_event.assert_called_once()
        s2.handle_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_target_id_falls_through(self):
        s1 = self._make_strategy("strat_a")
        runner = self._make_runner([s1])

        event = MagicMock()
        event.strategy_id = "nonexistent"
        event.symbol = "SYM1"
        event.meta = None
        event.ts = 1000

        await runner.process_event(event)

        # Falls through to full list iteration, but strategy_id check filters it out
        # strat_a won't be called because target_strat_id doesn't match
        s1.handle_event.assert_not_called()
