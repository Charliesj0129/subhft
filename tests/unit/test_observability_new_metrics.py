"""Tests for observability gap-closing metrics.

Three metrics introduced to close gaps surfaced by the debug team:
    - strategy_events_received_total{strategy_id} (Counter)
    - alias_resolution_coverage_ratio            (Gauge 0.0-1.0)
    - reconciliation_drift_streak{symbol}        (Gauge)

See commit adding these for wiring rationale.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

from hft_platform.observability.metrics import MetricsRegistry


def _fresh_registry() -> MetricsRegistry:
    MetricsRegistry._instance = None
    return MetricsRegistry.get()


def test_new_metrics_exist_with_expected_types_and_labels():
    registry = _fresh_registry()

    assert hasattr(registry, "strategy_events_received_total")
    assert isinstance(registry.strategy_events_received_total, Counter)
    # Label set: strategy_id
    assert registry.strategy_events_received_total._labelnames == ("strategy_id",)

    assert hasattr(registry, "alias_resolution_coverage_ratio")
    assert isinstance(registry.alias_resolution_coverage_ratio, Gauge)
    # No labels on the coverage gauge
    assert registry.alias_resolution_coverage_ratio._labelnames == ()

    assert hasattr(registry, "reconciliation_drift_streak")
    assert isinstance(registry.reconciliation_drift_streak, Gauge)
    assert registry.reconciliation_drift_streak._labelnames == ("symbol",)


def test_strategy_events_received_increments_on_dispatch():
    """Simulate a strategy dispatch via StrategyRunner and assert the counter increments."""
    from hft_platform.strategy.base import BaseStrategy
    from hft_platform.strategy.runner import StrategyRunner

    _fresh_registry()

    class _StubBus:
        cursor = 0

    class _DummyStrategy(BaseStrategy):
        def __init__(self):
            super().__init__(strategy_id="obs_test_strat", params={})
            self.symbols = {"TXFD6"}
            self.enabled = True
            self.calls = 0

        def handle_event(self, ctx, event):  # type: ignore[override]
            self.calls += 1
            return []

    # Instantiate with dummy bus + queue — we won't actually run the loop, just dispatch.
    import asyncio

    bus = _StubBus()
    runner = StrategyRunner(
        bus=bus,
        risk_queue=asyncio.Queue(maxsize=16),
        config_path="config/base/strategies.yaml",
    )
    # Clear any strategies loaded from config — we want an isolated strategy.
    runner.strategies = []
    runner._strat_executors = []
    runner._strat_index = {}

    strat = _DummyStrategy()
    runner.register(strat)

    # Grab initial counter value via exposed labels().
    counter = MetricsRegistry.get().strategy_events_received_total.labels(strategy_id="obs_test_strat")
    initial = counter._value.get()

    # Build a minimal tick-like event. StrategyRunner's process_event is async; run it.
    class _Ev:
        symbol = "TXFD6"
        meta = None
        ts = 0

    async def _drive():
        await runner.process_event(_Ev())

    asyncio.get_event_loop().run_until_complete(_drive()) if False else asyncio.run(_drive())

    assert strat.calls == 1, "Dummy strategy must have been dispatched"
    assert counter._value.get() == initial + 1.0


def test_alias_resolution_coverage_ratio_reflects_partial_coverage():
    """Simulate _propagate_alias_map with partial coverage and assert gauge reflects ratio."""
    _fresh_registry()

    # Minimal stand-in for MarketDataService._propagate_alias_map behaviour.
    # Broker client resolved 4 aliases; SymbolMetadata receives only 2 of them
    # (simulates a partial propagation race).
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    class _Client:
        alias_to_actual = {
            "TXFR1": "TXFE6",
            "TXFR2": "TXFJ6",
            "TMFR1": "TMFE6",
            "TMFC0": "TMFD6",
        }

    metadata = SymbolMetadata()
    # Only partial set propagated (2 of 4).
    metadata.set_alias_map({"TXFR1": "TXFE6", "TMFR1": "TMFE6"})

    configured = len(_Client.alias_to_actual)
    resolved = len(metadata.alias_to_actual)
    ratio = resolved / configured

    gauge = MetricsRegistry.get().alias_resolution_coverage_ratio
    gauge.set(ratio)

    assert configured == 4
    assert resolved == 2
    assert abs(gauge._value.get() - 0.5) < 1e-9

    # Full coverage case.
    metadata.set_alias_map({"TXFR2": "TXFJ6", "TMFC0": "TMFD6"})
    resolved = len(metadata.alias_to_actual)
    gauge.set(resolved / configured)
    assert resolved == 4
    assert abs(gauge._value.get() - 1.0) < 1e-9


def test_reconciliation_drift_streak_gauge_labels():
    """Gauge accepts per-symbol labels and tracks streak values."""
    registry = _fresh_registry()
    registry.reconciliation_drift_streak.labels(symbol="TXFD6").set(3)
    registry.reconciliation_drift_streak.labels(symbol="TMFD6").set(1)

    assert registry.reconciliation_drift_streak.labels(symbol="TXFD6")._value.get() == 3
    assert registry.reconciliation_drift_streak.labels(symbol="TMFD6")._value.get() == 1

    # Reset on drift resolution.
    registry.reconciliation_drift_streak.labels(symbol="TXFD6").set(0)
    assert registry.reconciliation_drift_streak.labels(symbol="TXFD6")._value.get() == 0
