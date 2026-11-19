"""P1-7: ``RiskEngine._drain_order_dlq`` must snapshot ``_order_dlq`` before the
clear-and-rebuild rebuild blocks so a re-entrant mutation (e.g. a rejection
callback that re-enqueues into the DLQ) cannot raise
``RuntimeError: deque mutated during iteration`` and cannot break LRU semantics.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    Side,
    StormGuardState,
)


def _make_intent(intent_id: int = 1, price: int = 100, qty: int = 1) -> OrderIntent:
    return OrderIntent(intent_id, "s1", "2330", IntentType.NEW, Side.BUY, price, qty, TIF.ROD, None, 0)


@pytest.fixture
def engine(tmp_path):
    from hft_platform.risk.engine import RiskEngine

    cfg = tmp_path / "risk.yaml"
    cfg.write_text("risk:\n  max_order_size: 100\n  max_position: 200\n  max_notional: 10000000\n")
    q_in: asyncio.Queue = asyncio.Queue()
    q_out: asyncio.Queue = asyncio.Queue(maxsize=4096)
    rejection_sink: asyncio.Queue = asyncio.Queue(maxsize=256)
    eng = RiskEngine(str(cfg), q_in, q_out, rejection_sink=rejection_sink)
    eng._dlq_drain_interval = 1
    return eng


def test_storm_clear_survives_reentrant_mutation(engine) -> None:
    """The HALT/STORM clear loop must use a snapshot so a sink callback that
    appends back into the DLQ does not cause iteration to raise."""
    cmds = [engine.create_command(_make_intent(i)) for i in range(5)]
    for cmd in cmds:
        engine._order_dlq.append((cmd, time.monotonic_ns()))

    engine.storm_guard.state = StormGuardState.STORM

    poison_cmd = engine.create_command(_make_intent(99))
    original_send = engine._send_dlq_rejection
    appended: list = []

    def _reentrant_send(cmd, reason):
        if not appended:
            appended.append(cmd)
            engine._order_dlq.append((poison_cmd, time.monotonic_ns()))
        original_send(cmd, reason)

    engine._send_dlq_rejection = _reentrant_send  # type: ignore[assignment]

    engine._drain_order_dlq()

    assert engine._rejection_sink.qsize() >= 1


def test_storm_clear_processes_full_snapshot_even_with_concurrent_append(engine) -> None:
    """All 3 seeded entries must receive rejection feedback even if a sink
    callback appends a 4th entry mid-iteration. The 4th must NOT be processed
    by the same drain (it's outside the snapshot)."""
    cmds = [engine.create_command(_make_intent(i)) for i in range(3)]
    for cmd in cmds:
        engine._order_dlq.append((cmd, time.monotonic_ns()))

    engine.storm_guard.state = StormGuardState.STORM

    extra_cmd = engine.create_command(_make_intent(42))
    original_send = engine._send_dlq_rejection
    call_count = {"n": 0}

    def _appending_send(cmd, reason):
        call_count["n"] += 1
        if call_count["n"] == 1:
            engine._order_dlq.append((extra_cmd, time.monotonic_ns()))
        original_send(cmd, reason)

    engine._send_dlq_rejection = _appending_send  # type: ignore[assignment]

    engine._drain_order_dlq()

    assert call_count["n"] == 3
    assert engine._rejection_sink.qsize() == 3


def test_drain_no_runtime_error_on_concurrent_clear(engine) -> None:
    """Stress: re-entrant append on every reject. Must complete without
    deque-mutated-during-iteration."""
    cmds = [engine.create_command(_make_intent(i)) for i in range(8)]
    for cmd in cmds:
        engine._order_dlq.append((cmd, time.monotonic_ns()))

    engine.storm_guard.state = StormGuardState.HALT

    extra = engine.create_command(_make_intent(900))
    original_send = engine._send_dlq_rejection

    def _every_send_appends(cmd, reason):
        try:
            engine._order_dlq.append((extra, time.monotonic_ns()))
        except (RuntimeError, AttributeError):
            pass
        original_send(cmd, reason)

    engine._send_dlq_rejection = _every_send_appends  # type: ignore[assignment]

    engine._drain_order_dlq()
    assert engine._rejection_sink.qsize() == 8
