"""A NEW order that timed out must leave a row in ``audit.orders_log``.

THESHOW, R47 (SIM), 2026-09-04..10-04: 54 fills carry strategy ``UNKNOWN`` and
never reach the platform position. Their orders were accepted by the broker
after the 3 s ``place_order`` timeout: no ``dispatched`` row exists for about
40 of them and nothing else was written either, so the intent that produced
the order could not be found. The phantom candidate that would claim the fill
is dropped 30 s after the timeout, again without a trace.

Two rows close that gap and change no behaviour:

* ``dispatch_failed`` -- a NEW whose broker call returned no trade, with the
  order key, the reason, and whether the call may have reached the broker;
* ``phantom_expired`` -- a phantom candidate dropped at its TTL, after which a
  late fill for that order arrives as ``UNKNOWN``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
)
from hft_platform.core import timebase
from hft_platform.order import adapter as adapter_module
from hft_platform.order.adapter import OrderAdapter, _PhantomEntry
from hft_platform.risk.storm_guard import StormGuardState

_STRATEGY = "R47_MAKER_TMF"


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg)


@pytest.fixture(autouse=True)
def _mock_infra():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        mm.get.return_value = MagicMock()
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _adapter(tmp_config: str, *, place_order: Any | None = None) -> OrderAdapter:
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
    if place_order is not None:
        client.place_order = place_order
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(maxsize=128),
        broker_client=client,
    )
    codec = MagicMock()
    codec.encode_side.return_value = "Buy"
    codec.encode_tif.return_value = "ROD"
    codec.encode_price_type.return_value = "LMT"
    adapter._broker_codec = codec
    adapter._add_to_dlq = AsyncMock()
    adapter._audit_writer = MagicMock()
    adapter.set_rejection_sink(asyncio.Queue(maxsize=64))
    return adapter


def _intent(intent_id: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=_STRATEGY,
        symbol="TMFI6",
        price=4623_9000,
        qty=1,
        side=Side.BUY,
        intent_type=IntentType.NEW,
        tif=TIF.LIMIT,
    )


def _command(intent_id: int = 1) -> OrderCommand:
    return OrderCommand(
        cmd_id=intent_id,
        intent=_intent(intent_id),
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


def _rows(adapter: OrderAdapter, event: str) -> list[dict[str, Any]]:
    return [c.args[0] for c in adapter._audit_writer.log_order.call_args_list if c.args[0].get("event") == event]


async def _dispatch_with_api_result(adapter: OrderAdapter, cmd: OrderCommand, result: Any) -> bool:
    async def _fake_call_api(op, fn, *args, **kwargs):
        return result

    with patch.object(adapter, "_call_api", _fake_call_api):
        return await adapter._dispatch_to_api(cmd)


@pytest.mark.asyncio
async def test_a_timed_out_place_order_is_audited_with_its_order_key_and_phantom_flag(tmp_config):
    release = threading.Event()

    def _hang(*_a: Any, **_kw: Any) -> None:
        release.wait(timeout=5.0)  # woken in the finally below; no fixed sleep

    adapter = _adapter(tmp_config, place_order=_hang)
    adapter._api_timeout_s = 0.05
    try:
        ok = await adapter._dispatch_to_api(_command(intent_id=7))
    finally:
        release.set()

    assert ok is False
    rows = _rows(adapter, "dispatch_failed")
    assert len(rows) == 1
    row = rows[0]
    assert row["order_key"] == f"{_STRATEGY}:7"
    assert row["intent_type"] == "NEW"
    assert row["symbol"] == "TMFI6"
    assert row["price"] == 4623_9000
    assert row["qty"] == 1
    assert row["cmd_id"] == 7
    assert row["error"] == "api_failure"
    assert row["phantom"] is True, "a mutating call that timed out may have reached the broker"


@pytest.mark.asyncio
async def test_a_guard_timeout_is_audited_but_not_flagged_phantom(tmp_config):
    adapter = _adapter(tmp_config)

    ok = await _dispatch_with_api_result(adapter, _command(intent_id=8), adapter_module._GUARD_TIMEOUT)

    assert ok is False
    rows = _rows(adapter, "dispatch_failed")
    assert len(rows) == 1
    assert rows[0]["error"] == "api_timeout"
    assert rows[0]["phantom"] is False, "the semaphore was never acquired: nothing reached the broker"


@pytest.mark.asyncio
async def test_a_failure_that_never_reached_the_broker_is_audited_without_phantom(tmp_config):
    adapter = _adapter(tmp_config)

    ok = await _dispatch_with_api_result(adapter, _command(intent_id=9), None)

    assert ok is False
    rows = _rows(adapter, "dispatch_failed")
    assert len(rows) == 1
    assert rows[0]["error"] == "api_failure"
    assert rows[0]["phantom"] is False


@pytest.mark.asyncio
async def test_a_successful_new_dispatch_writes_no_dispatch_failed_row(tmp_config):
    adapter = _adapter(tmp_config)

    ok = await _dispatch_with_api_result(adapter, _command(intent_id=10), MagicMock(name="trade"))

    assert ok is True
    assert _rows(adapter, "dispatch_failed") == []
    assert len(_rows(adapter, "dispatched")) == 1


@pytest.mark.asyncio
async def test_an_expired_phantom_is_audited_with_its_key_and_ttl(tmp_config):
    adapter = _adapter(tmp_config)
    intent = _intent(intent_id=42)
    key = f"{_STRATEGY}:42"
    aged = time.monotonic() - 999.0
    adapter._phantom_records[key] = [_PhantomEntry(monotonic_ts=aged, symbol="TMFI6", created_ns=0, intent=intent)]
    adapter._phantom_order_keys[key] = (aged, "TMFI6")
    adapter._phantom_intents[key] = intent

    released = await adapter.release_stale_phantom_pendings(ttl_s=30.0)

    assert released == 1
    rows = _rows(adapter, "phantom_expired")
    assert len(rows) == 1
    assert rows[0]["order_key"] == key
    assert rows[0]["symbol"] == "TMFI6"
    assert rows[0]["side"] == str(Side.BUY)
    assert rows[0]["error"] == "phantom_ttl_30s"


@pytest.mark.asyncio
async def test_a_phantom_inside_its_ttl_is_not_audited_as_expired(tmp_config):
    adapter = _adapter(tmp_config)
    intent = _intent(intent_id=43)
    key = f"{_STRATEGY}:43"
    fresh = time.monotonic()
    adapter._phantom_records[key] = [_PhantomEntry(monotonic_ts=fresh, symbol="TMFI6", created_ns=0, intent=intent)]
    adapter._phantom_order_keys[key] = (fresh, "TMFI6")
    adapter._phantom_intents[key] = intent

    released = await adapter.release_stale_phantom_pendings(ttl_s=30.0)

    assert released == 0
    assert _rows(adapter, "phantom_expired") == []


@pytest.mark.asyncio
async def test_a_failing_audit_writer_does_not_change_the_timeout_outcome(tmp_config):
    adapter = _adapter(tmp_config)
    adapter._audit_writer.log_order.side_effect = RuntimeError("audit queue gone")

    ok = await _dispatch_with_api_result(adapter, _command(intent_id=11), None)

    assert ok is False
    adapter._add_to_dlq.assert_awaited_once()
    assert adapter._rejection_sink.qsize() == 1, "the strategy still gets its pending slot back"
