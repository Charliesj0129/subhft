"""Regression test for Bug 25 (2026-04-17 DLQ drain reduce-only preservation).

Symptom: ``RiskEngine._drain_order_dlq`` at ``engine.py:673/690`` blanket-clears
ALL pending DLQ entries when StormGuard enters HALT/STORM. That includes any
cover orders that were approved pre-escalation and still need to unwind a stuck
position. Bug 21 fixed the live path (validators + StormGuard) to allow cover
orders; Bug 22/24 fixed Gateway/adapter. DLQ drain was the last layer still
dropping covers wholesale.

Fix: modify ``_drain_order_dlq`` to preserve reducing intents during HALT/STORM
clears and only drop opening intents. Downstream OrderAdapter (Bug 24) allows
the preserved covers through.

These tests exercise the batch-level behaviour where HALT arrives during drain.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)


def _make_intent(intent_id: int, side=Side.BUY, qty: int = 1):
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="R47_MAKER_TMF",
        symbol="TMFE6",
        intent_type=IntentType.NEW,
        side=side,
        price=377_000_000,
        qty=qty,
    )


def _make_cmd(intent, cmd_id: int = 1):
    return OrderCommand(
        intent=intent,
        cmd_id=cmd_id,
        deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=0,
    )


def _build_engine_with_position(short_position: int):
    """Build a bare RiskEngine exposing _drain_order_dlq with position provider."""
    from collections import deque

    from hft_platform.risk.engine import RiskEngine

    engine = RiskEngine.__new__(RiskEngine)
    engine._order_dlq = deque()
    engine._ORDER_DLQ_MAX = 256
    engine._dlq_ttl_ns = 30_000_000_000
    engine._dlq_drain_counter = 0
    engine._dlq_drain_interval = 1
    engine._rejection_sink = None
    engine.order_queue = asyncio.Queue(maxsize=128)
    engine.metrics = MagicMock()
    engine.validators = []
    engine._position_provider = lambda symbol, sid: (
        short_position if (symbol, sid) == ("TMFE6", "R47_MAKER_TMF") else 0
    )
    sg = MagicMock()
    sg.state = StormGuardState.HALT
    engine.storm_guard = sg
    return engine


class TestDlqDrainPreservesCoversOnHalt:
    """Bug 25: covers survive HALT/STORM blanket clear; openers are dropped."""

    def test_cover_preserved_openings_cleared_on_halt(self):
        """Short position -1; DLQ has [cover BUY, open SELL]. Only BUY survives."""
        engine = _build_engine_with_position(short_position=-1)
        cover = _make_cmd(_make_intent(1, side=Side.BUY), cmd_id=1)
        opener = _make_cmd(_make_intent(2, side=Side.SELL), cmd_id=2)

        now = time.monotonic_ns()
        engine._order_dlq.append((cover, now))
        engine._order_dlq.append((opener, now))

        engine._drain_order_dlq()

        # Cover should be preserved in DLQ or drained to order_queue.
        # Opener should be cleared.
        remaining_cmds = [c.cmd_id for c, _ in engine._order_dlq]
        drained_cmds = []
        while not engine.order_queue.empty():
            drained_cmds.append(engine.order_queue.get_nowait().cmd_id)

        assert 2 not in remaining_cmds + drained_cmds, "Opener must be cleared"
        assert 1 in remaining_cmds + drained_cmds, (
            "Cover must survive (preserved or drained)"
        )

    def test_all_openers_still_blanket_cleared_when_flat(self):
        """Backward compat: with position=0, all DLQ entries are opening → cleared."""
        engine = _build_engine_with_position(short_position=0)
        opener1 = _make_cmd(_make_intent(1, side=Side.SELL), cmd_id=1)
        opener2 = _make_cmd(_make_intent(2, side=Side.SELL), cmd_id=2)

        now = time.monotonic_ns()
        engine._order_dlq.append((opener1, now))
        engine._order_dlq.append((opener2, now))

        engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0, (
            "Opening orders must still be cleared (flat position)"
        )

    def test_helper_identifies_reducing_cover(self):
        """_cmd_reduces_position returns True for NEW BUY covering a short."""
        engine = _build_engine_with_position(short_position=-1)
        cover = _make_cmd(_make_intent(1, side=Side.BUY))
        assert engine._cmd_reduces_position(cover) is True

    def test_helper_identifies_opening(self):
        """_cmd_reduces_position returns False for opening SELL from flat."""
        engine = _build_engine_with_position(short_position=0)
        opener = _make_cmd(_make_intent(1, side=Side.SELL))
        assert engine._cmd_reduces_position(opener) is False

    def test_helper_returns_false_without_provider(self):
        """Defensive: no position_provider → conservative False (cleared on HALT)."""
        engine = _build_engine_with_position(short_position=-1)
        engine._position_provider = None
        cover = _make_cmd(_make_intent(1, side=Side.BUY))
        assert engine._cmd_reduces_position(cover) is False
