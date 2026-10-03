"""The HALT drains must tell the strategy what they dropped.

2026-09-29 05:35Z: the gateway allowed two reduce-only SELLs through the HALT
policy, then the ``order_queue`` drain in ``HFTSystem`` discarded both with
only ``drained_count += 1``. R47 had already counted them in
``_pending_sell``; nothing ever released the slot, so
``can_sell = pos - pending_sell > -max_pos`` stayed False for 79 h.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskFeedback,
    Side,
)
from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.system import HFTSystem


def _system(queue: asyncio.Queue | None) -> HFTSystem:
    system = HFTSystem.__new__(HFTSystem)
    system.strategy_runner = SimpleNamespace(_rejection_queue=queue)
    return system


def _intent(side: Side, intent_id: int = 7) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="R47_MAKER_TMF",
        symbol="TMFJ6",
        intent_type=IntentType.NEW,
        side=side,
        price=330_000_0,
        qty=1,
    )


def _command(side: Side) -> OrderCommand:
    return OrderCommand(
        cmd_id=1,
        intent=_intent(side),
        deadline_ns=0,
        storm_guard_state=StormGuardState.HALT,
    )


def _r47():
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    strat = R47MakerStrategy.__new__(R47MakerStrategy)
    strat._local_pos = {}
    strat._pending_buy = {"TMFJ6": 0}
    strat._pending_sell = {"TMFJ6": 2}
    strat._active_buy_oid = {}
    strat._active_sell_oid = {}
    strat._last_bid = {}
    strat._last_ask = {}
    strat._last_quote_ns = {}
    strat._seen_fill_ids = set()
    strat._FILL_DEDUP_MAX = 500
    return strat


def test_drained_order_command_emits_feedback_carrying_its_side():
    queue: asyncio.Queue = asyncio.Queue()
    _system(queue)._release_halt_drained(_command(Side.SELL))

    feedback = queue.get_nowait()
    assert isinstance(feedback, RiskFeedback)
    assert feedback.side == Side.SELL
    assert feedback.strategy_id == "R47_MAKER_TMF"
    assert feedback.symbol == "TMFJ6"
    assert feedback.intent_id == 7
    assert feedback.reason_code == "HALT_DRAINED"
    assert feedback.was_approved is False


def test_drained_typed_envelope_emits_feedback_from_the_tuple():
    queue: asyncio.Queue = asyncio.Queue()
    payload = ("typed_intent_v1", 9, "R47_MAKER_TMF", "TMFJ6", int(IntentType.NEW), int(Side.BUY)) + (0,) * 12
    _system(queue)._release_halt_drained(SimpleNamespace(payload=payload))

    feedback = queue.get_nowait()
    assert feedback.side == Side.BUY
    assert feedback.intent_id == 9


def test_two_drained_sells_unfreeze_r47_can_sell():
    """The incident, end to end: pending_sell=2 at pos=1 must clear."""
    queue: asyncio.Queue = asyncio.Queue()
    system = _system(queue)
    system._release_halt_drained(_command(Side.SELL))
    system._release_halt_drained(_command(Side.SELL))

    strat = _r47()
    assert not (1 - strat._pending_sell["TMFJ6"] > -1)  # frozen before the release
    while not queue.empty():
        strat.on_risk_feedback(queue.get_nowait())

    assert strat._pending_sell["TMFJ6"] == 0
    assert 1 - strat._pending_sell["TMFJ6"] > -1  # can_sell is True again


def test_item_without_a_side_is_not_fed_back_and_does_not_raise():
    queue: asyncio.Queue = asyncio.Queue()
    _system(queue)._release_halt_drained(SimpleNamespace(strategy_id="R47_MAKER_TMF"))

    assert queue.empty()


def test_full_rejection_queue_drops_the_feedback_without_raising():
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait("occupied")
    _system(queue)._release_halt_drained(_command(Side.SELL))

    assert queue.qsize() == 1


def test_missing_rejection_queue_is_a_noop():
    _system(None)._release_halt_drained(_command(Side.SELL))
