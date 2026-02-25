"""Tests for CE2-01 (OrderIntent fields) and CE2-02 (LocalIntentChannel)."""
import asyncio

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF
from hft_platform.gateway.channel import IntentEnvelope, LocalIntentChannel


def _make_intent(intent_id: int = 1, idempotency_key: str = "", ttl_ns: int = 0) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol="TSE:2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=1000000,
        qty=1,
        tif=TIF.LIMIT,
        idempotency_key=idempotency_key,
        ttl_ns=ttl_ns,
    )


# CE2-01: idempotency_key / ttl_ns defaults and round-trip

def test_order_intent_idempotency_key_default():
    intent = _make_intent()
    assert intent.idempotency_key == ""
    assert intent.ttl_ns == 0


def test_order_intent_idempotency_key_set():
    intent = _make_intent(idempotency_key="abc-123", ttl_ns=500_000_000)
    assert intent.idempotency_key == "abc-123"
    assert intent.ttl_ns == 500_000_000


def test_order_intent_backward_compat_positional():
    """Old callers without idempotency_key/ttl_ns still work."""
    intent = OrderIntent(
        intent_id=99,
        strategy_id="legacy",
        symbol="TSE:2330",
        intent_type=IntentType.CANCEL,
        side=Side.SELL,
        price=500000,
        qty=2,
    )
    assert intent.idempotency_key == ""
    assert intent.ttl_ns == 0


# CE2-02: LocalIntentChannel

@pytest.mark.asyncio
async def test_channel_submit_and_receive():
    ch = LocalIntentChannel(maxsize=10, ttl_ms=0)
    intent = _make_intent(1, "key-1")
    token = ch.submit_nowait(intent)
    assert token == "key-1"

    env = await ch.receive()
    assert isinstance(env, IntentEnvelope)
    assert env.intent is intent
    assert env.ack_token == "key-1"
    ch.task_done()


@pytest.mark.asyncio
async def test_channel_uses_intent_id_when_no_key():
    ch = LocalIntentChannel(maxsize=10, ttl_ms=0)
    intent = _make_intent(42)
    token = ch.submit_nowait(intent)
    assert token == "42"

    env = await ch.receive()
    assert env.ack_token == "42"
    ch.task_done()


@pytest.mark.asyncio
async def test_channel_queue_full_raises():
    ch = LocalIntentChannel(maxsize=2, ttl_ms=0)
    ch.submit_nowait(_make_intent(1))
    ch.submit_nowait(_make_intent(2))
    with pytest.raises(asyncio.QueueFull):
        ch.submit_nowait(_make_intent(3))


@pytest.mark.asyncio
async def test_channel_qsize():
    ch = LocalIntentChannel(maxsize=10, ttl_ms=0)
    assert ch.qsize() == 0
    ch.submit_nowait(_make_intent(1))
    assert ch.qsize() == 1
    ch.submit_nowait(_make_intent(2))
    assert ch.qsize() == 2


@pytest.mark.asyncio
async def test_channel_ttl_expired_routes_to_dlq():
    """Envelope older than TTL is skipped to DLQ; next valid envelope returned."""
    ch = LocalIntentChannel(maxsize=10, ttl_ms=1)  # 1ms TTL

    intent_old = _make_intent(1, "old")
    env_old = IntentEnvelope(
        intent=intent_old,
        enqueued_ns=0,  # epoch = very old
        ack_token="old",
    )
    intent_fresh = _make_intent(2, "fresh")

    # Manually put the stale envelope first
    ch._queue.put_nowait(env_old)
    # Then the fresh one
    ch.submit_nowait(intent_fresh)

    received = await asyncio.wait_for(ch.receive(), timeout=1.0)
    assert received.ack_token == "fresh"
    assert ch.dlq_size() == 1
    ch.task_done()


@pytest.mark.asyncio
async def test_channel_dlq_bounded():
    """DLQ has bounded size; oldest entries are dropped when full."""
    ch = LocalIntentChannel(maxsize=100, ttl_ms=5000, dlq_maxsize=2)
    # Submit 5 stale envelopes
    for i in range(5):
        ch._queue.put_nowait(IntentEnvelope(
            intent=_make_intent(i),
            enqueued_ns=0,  # epoch = expired
            ack_token=str(i),
        ))
    # Add one fresh
    ch.submit_nowait(_make_intent(99, "fresh"))

    received = await asyncio.wait_for(ch.receive(), timeout=1.0)
    assert received.ack_token == "fresh"
    # DLQ capped at 2 (deque maxlen)
    assert ch.dlq_size() <= 2
    ch.task_done()


@pytest.mark.asyncio
async def test_channel_submit_typed_and_materialize():
    ch = LocalIntentChannel(maxsize=10, ttl_ms=0)
    frame = (
        "typed_intent_v1",
        7,
        "alpha",
        "TSE:2330",
        int(IntentType.NEW),
        int(Side.BUY),
        1000000,
        2,
        int(TIF.LIMIT),
        "",
        123,
        456,
        "",
        "trace-1",
        "idem-7",
        0,
    )
    token = ch.submit_typed_nowait(frame)
    assert token == "idem-7"

    env = await ch.receive()
    assert isinstance(env, IntentEnvelope)
    assert env.intent.intent_id == 7
    assert env.intent.strategy_id == "alpha"
    assert env.intent.trace_id == "trace-1"
    assert env.ack_token == "idem-7"
    ch.task_done()
