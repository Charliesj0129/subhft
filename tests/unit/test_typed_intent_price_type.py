"""CR-1: typed_intent_v1 frame must carry price_type and assert arity.

Previously the 17-tuple fast path omitted `price_type`, causing a strategy's
`place_order(price_type="MKT")` to silently materialize as an LMT intent on the
risk/order side. This test pins down both fixes:

1. The frame emitted by `_intent_factory` carries price_type at index 17.
2. `submit_typed_nowait` rejects frames with wrong marker or insufficient arity.
3. `typed_frame_to_intent` propagates price_type end-to-end.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, Side, TIF
from hft_platform.gateway.channel import (
    LocalIntentChannel,
    typed_frame_to_intent,
    typed_frame_to_view,
)


def _make_runner():
    from hft_platform.strategy.runner import StrategyRunner

    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = StrategyRunner(MagicMock(), asyncio.Queue(), config_path="dummy")
    return runner


def test_intent_factory_emits_price_type_in_tuple():
    runner = _make_runner()
    runner._typed_intent_fastpath = True

    frame = runner._intent_factory(
        strategy_id="s1",
        symbol="2330",
        side=Side.BUY,
        price=5_050_000,
        qty=1,
        tif=TIF.LIMIT,
        intent_type=IntentType.NEW,
        price_type="MKT",
    )

    assert isinstance(frame, tuple)
    assert frame[0] == "typed_intent_v1"
    assert len(frame) >= 18, "frame must carry price_type at index 17"
    assert frame[17] == "MKT"


def test_typed_frame_to_intent_propagates_price_type():
    runner = _make_runner()
    runner._typed_intent_fastpath = True

    frame = runner._intent_factory(
        strategy_id="s1",
        symbol="2330",
        side=Side.BUY,
        price=5_050_000,
        qty=1,
        tif=TIF.LIMIT,
        intent_type=IntentType.NEW,
        price_type="MKT",
    )

    intent = typed_frame_to_intent(frame)
    assert intent.price_type == "MKT"


def test_legacy_frame_without_price_type_defaults_to_lmt():
    """Frames emitted before the schema bump carry only 17 items — treat as LMT."""
    legacy_frame = (
        "typed_intent_v1",
        1,  # intent_id
        "s1",  # strategy_id
        "2330",  # symbol
        int(IntentType.NEW),
        int(Side.BUY),
        5_050_000,
        1,
        int(TIF.LIMIT),
        "",  # target_order_id
        0,  # timestamp_ns
        0,  # source_ts_ns
        "",  # reason
        "",  # trace_id
        "",  # idempotency_key
        0,  # ttl_ns
        0,  # decision_price
    )
    intent = typed_frame_to_intent(legacy_frame)
    assert intent.price_type == "LMT"


def test_submit_typed_nowait_rejects_wrong_marker():
    chan = LocalIntentChannel(maxsize=4)
    with pytest.raises(ValueError, match="Invalid typed intent frame"):
        chan.submit_typed_nowait(("not_typed_intent", 1, "s1", "2330") + (0,) * 14)  # type: ignore[arg-type]


def test_submit_typed_nowait_rejects_short_arity():
    chan = LocalIntentChannel(maxsize=4)
    short_frame = ("typed_intent_v1", 1, "s1", "2330")  # 4 items
    with pytest.raises(ValueError, match="Invalid typed intent frame"):
        chan.submit_typed_nowait(short_frame)  # type: ignore[arg-type]
