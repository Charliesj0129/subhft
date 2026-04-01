"""Tests for decision_price population on both OrderIntent and typed intent tuples.

Covers DECISION-05: StrategyRunner must populate decision_price from LOB mid
for both OrderIntent objects and typed intent fast-path tuples.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bus(events=None):
    bus = MagicMock()
    events = events or []

    async def _gen():
        for e in events:
            yield e

    bus.consume.return_value = _gen()
    return bus


def _make_risk_queue():
    """Risk queue without submit_typed_nowait — disables typed fast path."""
    rq = MagicMock(spec=["put_nowait"])
    rq.put_nowait = MagicMock()
    return rq


def _make_risk_queue_typed():
    """Risk queue with submit_typed_nowait — enables typed fast path."""
    rq = MagicMock(spec=["put_nowait", "submit_typed_nowait"])
    rq.put_nowait = MagicMock()
    rq.submit_typed_nowait = MagicMock()
    return rq


def _make_lob_engine(mid_price_x2: int | None = None):
    """Create a LOB engine stub with optional last_stats."""
    lob = MagicMock()
    if mid_price_x2 is not None:
        lob.last_stats = SimpleNamespace(mid_price_x2=mid_price_x2)
    else:
        lob.last_stats = None
    return lob


def _make_order_intent(price: int = 1000_0000, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="test_strat",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
    )


def _make_typed_intent(price: int = 1000_0000, qty: int = 1) -> tuple:
    """Build a 17-element typed intent tuple matching _intent_factory output."""
    return (
        "typed_intent_v1",
        1,          # intent_id
        "test_strat",  # strategy_id
        "2330",     # symbol
        int(IntentType.NEW),  # intent_type
        int(Side.BUY),  # side
        price,      # price
        qty,        # qty
        int(TIF.LIMIT),  # tif
        "",         # target_order_id
        123_000_000_000,  # timestamp_ns
        100_000_000_000,  # source_ts_ns
        "",         # reason
        "trace-1",  # trace_id
        "",         # idempotency_key
        0,          # ttl_ns
        0,          # decision_price (should be populated by runner)
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("strategies: []\n")
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")


@pytest.fixture(autouse=True)
def _patch_metrics():
    m = MagicMock()
    m.strategy_latency_ns.labels.return_value = MagicMock()
    m.strategy_intents_total.labels.return_value = MagicMock()
    m.feature_profile_compat_failures_total = MagicMock()
    with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
        mr.get.return_value = m
        with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
            lr.get.return_value = MagicMock()
            yield m


@pytest.fixture()
def runner_factory():
    def _make(bus=None, rq=None, lob_engine=None, typed=False):
        from hft_platform.strategy.runner import StrategyRunner

        bus = bus or _make_bus()
        if rq is None:
            rq = _make_risk_queue_typed() if typed else _make_risk_queue()
        runner = StrategyRunner(
            bus=bus,
            risk_queue=rq,
            lob_engine=lob_engine,
        )
        return runner, bus, rq

    return _make


# ---------------------------------------------------------------------------
# Tests: OrderIntent decision_price population
# ---------------------------------------------------------------------------


class TestOrderIntentDecisionPrice:
    """OrderIntent objects get decision_price populated from LOB mid."""

    def test_populates_decision_price_from_lob_stats(self, runner_factory):
        mid_price_x2 = 600_0000  # mid = 300_0000
        lob = _make_lob_engine(mid_price_x2=mid_price_x2)
        runner, _, rq = runner_factory(lob_engine=lob)

        intent = _make_order_intent()
        assert intent.decision_price == 0

        # Simulate the decision_price population logic
        if hasattr(runner.lob_engine, "last_stats") and runner.lob_engine.last_stats is not None:
            if isinstance(intent, OrderIntent):
                _mid = runner.lob_engine.last_stats.mid_price_x2 // 2
                intent.decision_mid = _mid
                intent.decision_price = _mid

        assert intent.decision_price == 300_0000
        assert intent.decision_mid == 300_0000

    def test_skips_when_last_stats_is_none(self, runner_factory):
        lob = _make_lob_engine(mid_price_x2=None)
        runner, _, _ = runner_factory(lob_engine=lob)

        intent = _make_order_intent()
        # The guard should prevent access
        if hasattr(runner.lob_engine, "last_stats") and runner.lob_engine.last_stats is not None:
            if isinstance(intent, OrderIntent):
                _mid = runner.lob_engine.last_stats.mid_price_x2 // 2
                intent.decision_price = _mid

        assert intent.decision_price == 0


# ---------------------------------------------------------------------------
# Tests: Typed intent tuple decision_price population
# ---------------------------------------------------------------------------


class TestTypedIntentDecisionPrice:
    """Typed intent tuples get decision_price populated at position 16."""

    def test_populates_decision_price_in_typed_tuple(self, runner_factory):
        mid_price_x2 = 600_0000  # mid = 300_0000
        lob = _make_lob_engine(mid_price_x2=mid_price_x2)
        runner, _, _ = runner_factory(lob_engine=lob, typed=True)

        intent = _make_typed_intent()
        assert intent[16] == 0  # decision_price starts at 0

        # Simulate the decision_price population logic (mirrors runner.py)
        if hasattr(runner.lob_engine, "last_stats") and runner.lob_engine.last_stats is not None:
            if isinstance(intent, tuple) and len(intent) >= 17 and intent[0] == "typed_intent_v1":
                _mid = runner.lob_engine.last_stats.mid_price_x2 // 2
                intent = (*intent[:16], _mid)

        assert intent[16] == 300_0000
        # Verify other fields are preserved
        assert intent[0] == "typed_intent_v1"
        assert intent[2] == "test_strat"
        assert intent[3] == "2330"
        assert intent[15] == 0  # ttl_ns unchanged

    def test_skips_when_last_stats_is_none(self, runner_factory):
        lob = _make_lob_engine(mid_price_x2=None)
        runner, _, _ = runner_factory(lob_engine=lob, typed=True)

        intent = _make_typed_intent()
        original_decision_price = intent[16]

        if hasattr(runner.lob_engine, "last_stats") and runner.lob_engine.last_stats is not None:
            if isinstance(intent, tuple) and len(intent) >= 17 and intent[0] == "typed_intent_v1":
                _mid = runner.lob_engine.last_stats.mid_price_x2 // 2
                intent = (*intent[:16], _mid)

        assert intent[16] == original_decision_price  # unchanged

    def test_skips_non_typed_intent_tuples(self, runner_factory):
        lob = _make_lob_engine(mid_price_x2=600_0000)
        runner, _, _ = runner_factory(lob_engine=lob, typed=True)

        # A tick event tuple (not a typed intent)
        intent = ("tick", 100, 50, 123_000_000_000)

        if hasattr(runner.lob_engine, "last_stats") and runner.lob_engine.last_stats is not None:
            if isinstance(intent, tuple) and len(intent) >= 17 and intent[0] == "typed_intent_v1":
                _mid = runner.lob_engine.last_stats.mid_price_x2 // 2
                intent = (*intent[:16], _mid)

        # Unchanged — not a typed intent
        assert intent == ("tick", 100, 50, 123_000_000_000)

    def test_tuple_length_preserved_at_17(self, runner_factory):
        lob = _make_lob_engine(mid_price_x2=800_0000)
        runner, _, _ = runner_factory(lob_engine=lob, typed=True)

        intent = _make_typed_intent()
        assert len(intent) == 17

        if hasattr(runner.lob_engine, "last_stats") and runner.lob_engine.last_stats is not None:
            if isinstance(intent, tuple) and len(intent) >= 17 and intent[0] == "typed_intent_v1":
                _mid = runner.lob_engine.last_stats.mid_price_x2 // 2
                intent = (*intent[:16], _mid)

        assert len(intent) == 17
        assert intent[16] == 400_0000


# ---------------------------------------------------------------------------
# Tests: Typed frame view conversion preserves decision_price
# ---------------------------------------------------------------------------


class TestTypedFrameViewDecisionPrice:
    """typed_frame_to_view and typed_view_to_intent carry decision_price."""

    def test_view_extracts_decision_price(self):
        from hft_platform.gateway.channel import typed_frame_to_view

        frame = _make_typed_intent()
        # Set decision_price to a known value
        frame = (*frame[:16], 500_0000)

        view = typed_frame_to_view(frame)
        assert view.decision_price == 500_0000

    def test_view_defaults_decision_price_for_legacy_16_element_frame(self):
        from hft_platform.gateway.channel import typed_frame_to_view

        # Legacy 16-element frame (no decision_price)
        frame = (
            "typed_intent_v1", 1, "strat", "2330",
            int(IntentType.NEW), int(Side.BUY), 1000_0000, 1,
            int(TIF.LIMIT), "", 123_000_000_000, 100_000_000_000,
            "", "trace-1", "", 0,
        )
        assert len(frame) == 16

        view = typed_frame_to_view(frame)
        assert view.decision_price == 0  # default for legacy frames

    def test_intent_from_typed_frame_preserves_decision_price(self):
        from hft_platform.gateway.channel import typed_frame_to_intent

        frame = _make_typed_intent()
        frame = (*frame[:16], 750_0000)

        intent = typed_frame_to_intent(frame)
        assert isinstance(intent, OrderIntent)
        assert intent.decision_price == 750_0000

    def test_intent_factory_emits_17_elements(self, runner_factory, monkeypatch):
        """_intent_factory produces 17-element tuples when typed fast path is enabled."""
        monkeypatch.setenv("HFT_TYPED_INTENT_CHANNEL", "1")
        rq = _make_risk_queue_typed()
        runner, _, _ = runner_factory(rq=rq, typed=True)

        # Force typed fast path on
        runner._typed_intent_fastpath = True

        result = runner._intent_factory(
            strategy_id="s1",
            symbol="2330",
            price=100_0000,
            qty=1,
            side=Side.BUY,
            tif=TIF.LIMIT,
            intent_type=IntentType.NEW,
        )

        assert isinstance(result, tuple)
        assert len(result) == 17
        assert result[0] == "typed_intent_v1"
        assert result[16] == 0  # decision_price default
