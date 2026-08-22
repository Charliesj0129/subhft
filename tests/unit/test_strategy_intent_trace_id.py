"""Every OrderIntent must carry a trace id, including ones born of tuple events.

Measured on THESHOW 2026-08-22: ``hft.fills`` had 13 rows since the last deploy
with ``client_order_id`` and ``strategy_id`` populated on all 13 and
``trace_id`` populated on **0**. The propagation chain itself was intact --
``order/adapter.py`` stamps ``_cmd_trace_id_map[order_key]`` and
``execution/router.py`` copies it onto the ``FillEvent`` -- but the adapter
guards its write with ``if _t:`` and ``_t`` was always empty.

The chain died at the source: ``_extract_event_trace`` only builds a trace id
in its ``meta`` branch, from ``meta.seq``. The hot path dispatches tuples
(``("tick", ...)``, ``("bidask", ...)``, ``("lobstats", ...)``), which take a
different branch and produced ``""``. With an empty trace id
``OrderExplanationAssembler.register`` drops the registration outright, so no
L8 explanation row could ever be produced from live trading.

The fix must not pay for itself on every tick: the id is built in
``_intent_factory`` (per intent) rather than in ``_extract_event_trace`` (per
event), which is what the allocation test below pins.

A first version keyed the id on ``tag:symbol:source_ts_ns`` alone. That merged
distinct events: the LOB rejects only timestamps that go *backwards*, so two
ticks on one symbol may share an exchange timestamp, and an event with no
timestamp falls back to ``timebase.now_ns()`` -- a clock, not an identity. Two
unrelated decisions would then have produced orders under one trace and any
incident timeline built from it would have merged them. A per-event counter
now carries the uniqueness; the timestamp stays only so ids remain distinct
across process restarts, which the counter cannot do on its own.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from hft_platform.contracts.strategy import IntentType
from hft_platform.strategy.runner import StrategyRunner


def _runner() -> StrategyRunner:
    """A StrategyRunner with only the fields _intent_factory and the trace path touch."""
    runner = StrategyRunner.__new__(StrategyRunner)
    runner._intent_seq = 0
    runner._current_source_ts_ns = 0
    runner._current_trace_id = ""
    runner._current_trace_tag = ""
    runner._current_event_seq = 0
    runner._current_contract = None
    runner._typed_intent_fastpath = False
    runner._default_intent_ttl_ns = 0
    runner.symbol_metadata = types.SimpleNamespace(tick_size_scaled=lambda _symbol: 0)
    return runner


def _make(runner: StrategyRunner, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "strategy_id": "R47_MAKER_TMF",
        "symbol": "TMFI6",
        "side": 1,
        "price": 21_500_000,
        "qty": 1,
        "tif": 0,
        "intent_type": IntentType.NEW,
    }
    kwargs.update(overrides)
    return runner._intent_factory(**kwargs)


class TestTupleEventsProduceATraceId:
    @pytest.mark.parametrize("tag,ts_index,length", [("tick", 7, 9), ("bidask", 4, 6), ("lobstats", 2, 4)])
    def test_each_hot_path_tuple_shape_yields_a_non_empty_trace_id(self, tag: str, ts_index: int, length: int) -> None:
        runner = _runner()
        event: list[Any] = [tag] + [0] * (length - 1)
        event[ts_index] = 1_787_329_000_000_000_000
        source_ts_ns, trace_id = runner._extract_event_trace(tuple(event))

        # The event itself still carries no id -- that part is unchanged.
        assert trace_id == ""
        # ...but the tag is now retained, so the intent can build one.
        assert runner._current_trace_tag == tag
        runner._current_source_ts_ns = source_ts_ns
        runner._current_trace_id = trace_id
        intent = _make(runner)
        assert intent.trace_id
        assert intent.trace_id.startswith(f"{tag}:TMFI6:")

    def test_the_trace_id_carries_the_events_source_timestamp(self) -> None:
        runner = _runner()
        ts = 1_787_329_000_000_000_000
        event = ("tick", 0, 0, 0, 0, 0, 0, ts, 0)
        source_ts_ns, trace_id = runner._extract_event_trace(event)
        runner._current_source_ts_ns = source_ts_ns
        runner._current_trace_id = trace_id
        assert _make(runner).trace_id == f"tick:TMFI6:{ts}:1"

    def test_two_intents_from_one_event_share_the_trace_id(self) -> None:
        """trace_id identifies the causing event, so a two-sided quote shares one."""
        runner = _runner()
        event = ("bidask", 0, 0, 0, 1_787_329_000_000_000_000, 0)
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(event)
        bid = _make(runner, side=1)
        ask = _make(runner, side=2)
        assert bid.trace_id  # two empty strings are also "equal" -- pin non-empty first
        assert bid.trace_id == ask.trace_id
        assert bid.intent_id != ask.intent_id

    def test_different_symbols_in_the_same_nanosecond_do_not_collide(self) -> None:
        runner = _runner()
        event = ("tick", 0, 0, 0, 0, 0, 0, 1_787_329_000_000_000_000, 0)
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(event)
        assert _make(runner, symbol="TMFI6").trace_id != _make(runner, symbol="MXFI6").trace_id

    def test_consecutive_events_get_different_trace_ids(self) -> None:
        runner = _runner()
        seen = set()
        for ts in (1_787_329_000_000_000_000, 1_787_329_000_000_000_001):
            runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(
                ("tick", 0, 0, 0, 0, 0, 0, ts, 0)
            )
            seen.add(_make(runner).trace_id)
        assert len(seen) == 2


class TestDistinctEventsNeverShareATraceId:
    """The regressions for the collision the first version of this fix had."""

    def test_two_events_with_the_same_timestamp_do_not_share_a_trace_id(self) -> None:
        """Equal exchange timestamps are legal: the LOB only rejects backwards ones."""
        runner = _runner()
        ts = 1_787_329_000_000_000_000
        first = ("tick", 0, 0, 0, 0, 0, 0, ts, 11)
        second = ("tick", 0, 0, 0, 0, 0, 0, ts, 22)

        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(first)
        a = _make(runner).trace_id
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(second)
        b = _make(runner).trace_id

        assert a and b
        assert a != b, "two distinct events collapsed into one trace"

    def test_events_without_a_timestamp_do_not_share_a_trace_id(self, monkeypatch) -> None:
        """The now_ns() fallback is a clock; a frozen clock must not merge events."""
        from hft_platform.core import timebase

        monkeypatch.setattr(timebase, "now_ns", lambda: 1_787_329_000_000_000_000)
        runner = _runner()
        untimed = ("tick", 0, 0, 0, 0, 0, 0, 0, 0)

        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(untimed)
        a = _make(runner).trace_id
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(untimed)
        b = _make(runner).trace_id

        assert a and b
        assert a != b

    def test_the_event_counter_advances_once_per_event_not_per_intent(self) -> None:
        runner = _runner()
        event = ("bidask", 0, 0, 0, 1_787_329_000_000_000_000, 0)
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(event)
        assert runner._current_event_seq == 1
        _make(runner, side=1)
        _make(runner, side=2)
        assert runner._current_event_seq == 1, "the counter must not move per intent"
        runner._extract_event_trace(event)
        assert runner._current_event_seq == 2


class TestExistingBehaviourIsPreserved:
    def test_an_event_with_meta_still_uses_its_own_seq(self) -> None:
        """The meta branch already worked; it must keep winning over the fallback."""
        runner = _runner()
        event = types.SimpleNamespace(
            meta=types.SimpleNamespace(local_ts=1_787_329_000_000_000_000, seq=4242, topic="ticks")
        )
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(event)
        assert runner._current_trace_id == "ticks:4242"
        assert _make(runner).trace_id == "ticks:4242"

    def test_an_explicit_trace_id_argument_still_wins(self) -> None:
        runner = _runner()
        runner._current_trace_tag = "tick"
        assert _make(runner, trace_id="explicit-id").trace_id == "explicit-id"

    def test_a_meta_event_without_seq_falls_back_to_its_topic(self) -> None:
        runner = _runner()
        event = types.SimpleNamespace(
            meta=types.SimpleNamespace(local_ts=1_787_329_000_000_000_000, seq=None, topic="ticks")
        )
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(event)
        assert _make(runner).trace_id == "ticks:TMFI6:1787329000000000000:1"

    def test_an_unrecognised_event_still_yields_a_usable_trace_id(self) -> None:
        """Fail-open on the id, never empty: an empty one is silently dropped downstream."""

        class OddEvent:
            local_ts = 1_787_329_000_000_000_000

        runner = _runner()
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(OddEvent())
        assert _make(runner).trace_id == "OddEvent:TMFI6:1787329000000000000:1"


class TestTheTypedFastPathCarriesItToo:
    def test_typed_intent_tuple_carries_the_derived_trace_id(self) -> None:
        runner = _runner()
        runner._typed_intent_fastpath = True
        runner._current_source_ts_ns, runner._current_trace_id = runner._extract_event_trace(
            ("tick", 0, 0, 0, 0, 0, 0, 1_787_329_000_000_000_000, 0)
        )
        intent = _make(runner)
        assert intent[0] == "typed_intent_v1"
        # index 13 is trace_id in the typed_intent_v1 layout
        assert intent[13] == "tick:TMFI6:1787329000000000000:1"


class TestPerEventCostIsUnchanged:
    def test_extracting_a_trace_from_a_tuple_event_allocates_no_string(self) -> None:
        """The tag is stored by reference; formatting is deferred to _intent_factory.

        Building the id inside _extract_event_trace would have put one f-string
        on every tick -- a per-tick allocation, which the hot path forbids.
        """
        runner = _runner()
        event = ("tick", 0, 0, 0, 0, 0, 0, 1_787_329_000_000_000_000, 0)
        runner._extract_event_trace(event)
        assert runner._current_trace_tag is event[0]
