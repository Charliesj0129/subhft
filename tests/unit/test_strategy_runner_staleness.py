"""Tests for event staleness guard in StrategyRunner.process_event()."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _make_bus():
    bus = MagicMock()

    async def _gen():
        return
        yield  # make it an async generator

    bus.consume.return_value = _gen()
    return bus


def _make_risk_queue():
    rq = MagicMock(spec=["put_nowait"])
    rq.put_nowait = MagicMock()
    return rq


def _make_event(ts_ns: int, symbol: str = "TSMC") -> SimpleNamespace:
    """Create a minimal market event with a .ts timestamp field."""
    return SimpleNamespace(symbol=symbol, ts=ts_ns)


class _FakeStrategy:
    def __init__(self, sid: str = "strat_a", symbols=None):
        self.strategy_id = sid
        self.symbols = set(symbols) if symbols else {"TSMC"}
        self.enabled = True
        self.required_features = []
        self.required_feature_profile = None
        self._calls: list = []

    def handle_event(self, ctx, event):
        self._calls.append((ctx, event))
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("strategies: []\n")
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")


@pytest.fixture()
def mock_metrics():
    m = MagicMock()
    m.strategy_latency_ns.labels.return_value = MagicMock()
    m.strategy_intents_total.labels.return_value = MagicMock()
    m.feature_profile_compat_failures_total = MagicMock()
    m.strategy_timeout_total.labels.return_value = MagicMock()
    m.strategy_circuit_break_total.labels.return_value = MagicMock()
    m.stale_event_skip_total = MagicMock()
    return m


@pytest.fixture()
def make_runner(mock_metrics, monkeypatch):
    def _factory(threshold_ms: str = "500"):
        monkeypatch.setenv("HFT_STALE_EVENT_THRESHOLD_MS", threshold_ms)
        with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
            mr.get.return_value = mock_metrics
            with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
                lr.get.return_value = MagicMock()
                from hft_platform.strategy.runner import StrategyRunner

                bus = _make_bus()
                rq = _make_risk_queue()
                runner = StrategyRunner(bus, rq)
                return runner, mock_metrics

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_event_skipped(make_runner):
    """An event older than threshold is skipped; strategy is not called."""
    runner, _ = make_runner(threshold_ms="1")  # 1 ms threshold
    strat = _FakeStrategy()
    runner.register(strat)

    now_ns = 1_000_000_000_000  # arbitrary now
    old_event_ns = now_ns - 2_000_000  # 2ms old → stale

    with patch("hft_platform.strategy.runner.timebase") as tb:
        tb.now_ns.return_value = now_ns
        await runner.process_event(_make_event(ts_ns=old_event_ns))

    assert strat._calls == [], "Strategy should NOT be called for a stale event"


@pytest.mark.asyncio
async def test_fresh_event_not_skipped(make_runner):
    """A fresh event (within threshold) is dispatched to strategies normally."""
    runner, _ = make_runner(threshold_ms="500")  # 500ms threshold
    strat = _FakeStrategy()
    runner.register(strat)

    now_ns = 1_000_000_000_000
    fresh_event_ns = now_ns - 100_000  # 0.1ms old → fresh

    with patch("hft_platform.strategy.runner.timebase") as tb:
        tb.now_ns.return_value = now_ns
        await runner.process_event(_make_event(ts_ns=fresh_event_ns))

    assert len(strat._calls) == 1, "Strategy SHOULD be called for a fresh event"


@pytest.mark.asyncio
async def test_staleness_counter_increments(make_runner):
    """_stale_event_skip_total increments for each stale event."""
    runner, _ = make_runner(threshold_ms="1")
    strat = _FakeStrategy()
    runner.register(strat)

    now_ns = 1_000_000_000_000
    old_event_ns = now_ns - 5_000_000  # 5ms old

    assert runner._stale_event_skip_total == 0

    with patch("hft_platform.strategy.runner.timebase") as tb:
        tb.now_ns.return_value = now_ns
        await runner.process_event(_make_event(ts_ns=old_event_ns))
        await runner.process_event(_make_event(ts_ns=old_event_ns))

    assert runner._stale_event_skip_total == 2


@pytest.mark.asyncio
async def test_zero_source_ts_not_skipped(make_runner):
    """An event with ts=0 should NOT be skipped.

    _extract_event_trace falls back to now_ns() when ts=0, making
    the event always appear fresh.
    """
    runner, _ = make_runner(threshold_ms="1")  # very tight threshold
    strat = _FakeStrategy()
    runner.register(strat)

    now_ns = 1_000_000_000_000

    with patch("hft_platform.strategy.runner.timebase") as tb:
        tb.now_ns.return_value = now_ns
        # ts=0 → _extract_event_trace returns now_ns → age ≈ 0 → not stale
        await runner.process_event(_make_event(ts_ns=0))

    # The fallback path sets source_ts_ns = now_ns(), so event_age_ns ≈ 0,
    # which is below threshold.  Strategy must be called.
    assert len(strat._calls) == 1, "Event with ts=0 should not be skipped"


@pytest.mark.asyncio
async def test_threshold_configurable_via_env(tmp_path, monkeypatch, mock_metrics):
    """HFT_STALE_EVENT_THRESHOLD_MS env var controls the staleness threshold."""
    monkeypatch.setenv("HFT_STALE_EVENT_THRESHOLD_MS", "100")  # 100ms
    monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("strategies: []\n")
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")

    with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
        mr.get.return_value = mock_metrics
        with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
            lr.get.return_value = MagicMock()
            from hft_platform.strategy.runner import StrategyRunner

            runner = StrategyRunner(_make_bus(), _make_risk_queue())

    # Threshold should be 100ms * 1_000_000 = 100_000_000 ns
    assert runner._stale_event_threshold_ns == 100 * 1_000_000


@pytest.mark.asyncio
async def test_prometheus_metric_incremented_on_skip(make_runner):
    """stale_event_skip_total Prometheus counter is incremented on skip."""
    runner, mock_metrics_obj = make_runner(threshold_ms="1")
    strat = _FakeStrategy()
    runner.register(strat)

    now_ns = 1_000_000_000_000
    old_event_ns = now_ns - 5_000_000  # 5ms old

    with patch("hft_platform.strategy.runner.timebase") as tb:
        tb.now_ns.return_value = now_ns
        await runner.process_event(_make_event(ts_ns=old_event_ns))

    mock_metrics_obj.stale_event_skip_total.inc.assert_called_once()
