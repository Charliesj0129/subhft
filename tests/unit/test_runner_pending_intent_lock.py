"""P1-9: per-strategy ``asyncio.Lock`` for ``_strategy_pending_intents``.

The read-modify-write pattern for the pending-intent counter spans an
``await`` boundary (the metric ``int_m.inc`` is sync but two coroutines for
the same ``sid`` can interleave their reads/writes). The lock guarantees no
counts are lost.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.strategy.runner import StrategyRunner


def _make_runner() -> StrategyRunner:
    """Construct a bare StrategyRunner with only the slots needed for the
    pending-intent counter machinery, bypassing the full bus/strategy
    bootstrap."""
    runner = StrategyRunner.__new__(StrategyRunner)
    runner._strategy_pending_intents = {}
    runner._pending_intent_locks = {}
    return runner


@pytest.mark.asyncio
async def test_get_pending_lock_returns_same_lock_per_sid():
    runner = _make_runner()
    lock_a = runner._get_pending_lock("strat_a")
    lock_b = runner._get_pending_lock("strat_a")
    assert lock_a is lock_b
    lock_other = runner._get_pending_lock("strat_b")
    assert lock_other is not lock_a


@pytest.mark.asyncio
async def test_concurrent_increments_no_lost_writes():
    """Fire 50 concurrent tasks each incrementing the pending counter by 1
    for the same ``sid``. Without the lock, the read-then-write pattern
    around an ``await`` boundary loses increments. With the lock, the final
    counter equals the sum of contributions."""
    runner = _make_runner()
    sid = "strat_x"
    contributors = 50
    per_task = 1

    async def _bump() -> None:
        async with runner._get_pending_lock(sid):
            # Force a yield between read and write to expose any race that
            # would survive without the lock — if two tasks share the lock
            # this still serialises correctly.
            current = runner._strategy_pending_intents.get(sid, 0)
            await asyncio.sleep(0)
            runner._strategy_pending_intents[sid] = current + per_task

    await asyncio.gather(*[_bump() for _ in range(contributors)])
    assert runner._strategy_pending_intents[sid] == contributors * per_task


@pytest.mark.asyncio
async def test_increment_and_drain_serialised():
    """50 increments and 1 drain interleaved: the drain must observe a
    counter consistent with the *number of completed increments at drain
    time* and the post-drain state must equal the sum of *post-drain
    increments only*. This proves no count is lost across the
    increment+drain boundary."""
    runner = _make_runner()
    sid = "strat_y"
    contributors = 50
    int_m = MagicMock()
    drained_at: list[int] = []

    async def _bump() -> None:
        async with runner._get_pending_lock(sid):
            runner._strategy_pending_intents[sid] = runner._strategy_pending_intents.get(sid, 0) + 1

    async def _drain_once() -> None:
        # Yield first so some increments race ahead.
        await asyncio.sleep(0)
        async with runner._get_pending_lock(sid):
            value = runner._strategy_pending_intents.get(sid, 0)
            if value:
                int_m.inc(value)
                runner._strategy_pending_intents[sid] = 0
                drained_at.append(value)

    await asyncio.gather(_drain_once(), *[_bump() for _ in range(contributors)])
    # Total = drained + remaining must equal contributors.
    drained_sum = sum(drained_at)
    remaining = runner._strategy_pending_intents.get(sid, 0)
    assert drained_sum + remaining == contributors
    # int_m.inc must have been called with the same totals as drained_at.
    inc_args = [call.args[0] for call in int_m.inc.call_args_list]
    assert inc_args == drained_at


@pytest.mark.asyncio
async def test_setdefault_lock_construction_is_safe_under_race():
    """Lazily creating the lock via ``setdefault`` is safe even if two
    coroutines call ``_get_pending_lock`` for the same sid simultaneously
    — only one ``asyncio.Lock`` survives in the dict."""
    runner = _make_runner()
    sid = "strat_z"

    async def _grab() -> asyncio.Lock:
        return runner._get_pending_lock(sid)

    locks = await asyncio.gather(*[_grab() for _ in range(10)])
    # All 10 references must point to the same lock instance.
    first = locks[0]
    assert all(lock is first for lock in locks)
    assert runner._pending_intent_locks[sid] is first
