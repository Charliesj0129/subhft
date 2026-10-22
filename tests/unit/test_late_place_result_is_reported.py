"""The answer to a timed-out ``place_order`` must be reported, not thrown away.

THESHOW, R47 (SIM), 2026-10-05: all 204 ``place_order`` calls that hit the 3 s
wrapper timeout were accepted by the broker anyway -- 204 ``UNKNOWN`` SUBMITTED
rows, one for one, 3.0-134.8 s after the call (median 6.7 s, 16 later than the 30 s
phantom TTL). ``_run_blocking_call`` keeps the SDK thread running after the
timeout and the Trade comes back to the event loop, where ``_set_result`` sees the
future already done and drops it. Nothing recorded that the answer ever arrived.

A late result is counted (``place_order_late_result_total{kind}``), logged and
audited (``late_result``, joinable to ``dispatch_failed`` on ``order_key``). What is
then DONE with a Trade that carries broker ids -- bind, cancel, hand the slot back --
is covered by ``test_phantom_bound_to_late_trade.py``; a Trade WITHOUT ids stays
observe-only, which is what is pinned here.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.order import adapter as adapter_module
from hft_platform.order.adapter import OrderAdapter
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


def _adapter(tmp_config: str, *, place_order: Any) -> OrderAdapter:
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
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
    adapter._api_timeout_s = 0.05
    return adapter


def _intent(intent_id: int = 7) -> OrderIntent:
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


def _command(intent_id: int = 7) -> OrderCommand:
    return OrderCommand(
        cmd_id=intent_id,
        intent=_intent(intent_id),
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


def _rows(adapter: OrderAdapter, event: str) -> list[dict[str, Any]]:
    return [c.args[0] for c in adapter._audit_writer.log_order.call_args_list if c.args[0].get("event") == event]


async def _until(predicate: Any, timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


class _LateBroker:
    """A place_order that answers only when released -- i.e. after the wrapper timed out."""

    def __init__(self, outcome: Any) -> None:
        self.release = threading.Event()
        self.called = threading.Event()
        self._outcome = outcome

    def __call__(self, *_a: Any, **_kw: Any) -> Any:
        self.called.set()
        self.release.wait(timeout=5.0)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


# --------------------------------------------------------------------------- #
# End to end through _call_api                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_late_place_result_is_reported_not_dropped(tmp_config):
    broker = _LateBroker({"order": {"ordno": "A1B2C", "seqno": "000123"}})
    adapter = _adapter(tmp_config, place_order=broker)

    ok = await adapter._dispatch_to_api(_command(intent_id=7))
    assert ok is False, "the call timed out: the dispatch itself is unchanged"
    broker.release.set()  # the broker answers after the timeout
    await _until(lambda: _rows(adapter, "late_result"))

    (row,) = _rows(adapter, "late_result")
    assert row["order_key"] == f"{_STRATEGY}:7", "joinable to the dispatch_failed row"
    assert row["intent_type"] == "NEW"
    assert row["symbol"] == "TMFI6"
    assert row["price"] == 4623_9000
    assert row["strategy_id"] == _STRATEGY
    assert "kind=trade" in row["details"]
    assert "has_ids=True" in row["details"]
    assert "elapsed_s=" in row["details"]
    adapter.metrics.place_order_late_result_total.labels.assert_called_with(kind="trade")


@pytest.mark.asyncio
async def test_a_late_trade_without_broker_ids_is_reported_as_such(tmp_config):
    broker = _LateBroker({})
    adapter = _adapter(tmp_config, place_order=broker)

    await adapter._dispatch_to_api(_command())
    broker.release.set()
    await _until(lambda: _rows(adapter, "late_result"))

    assert "has_ids=False" in _rows(adapter, "late_result")[0]["details"]


@pytest.mark.asyncio
async def test_a_late_broker_error_is_reported_with_its_type(tmp_config):
    broker = _LateBroker(RuntimeError("Session error"))
    adapter = _adapter(tmp_config, place_order=broker)

    await adapter._dispatch_to_api(_command())
    broker.release.set()
    await _until(lambda: _rows(adapter, "late_result"))

    (row,) = _rows(adapter, "late_result")
    assert "kind=error" in row["details"]
    assert row["error"] == "RuntimeError"
    adapter.metrics.place_order_late_result_total.labels.assert_called_with(kind="error")


@pytest.mark.asyncio
async def test_a_late_trade_without_ids_changes_no_state(tmp_config):
    """Nothing can be bound or cancelled without a broker id: no Trade is kept, no id
    is bound, nothing is cancelled and no pending slot is released by the answer."""
    broker = _LateBroker({})
    adapter = _adapter(tmp_config, place_order=broker)
    await adapter._dispatch_to_api(_command())
    sink = adapter._rejection_sink
    released_before = sink.qsize()
    phantoms_before = set(adapter.get_phantom_candidates())
    map_before = dict(adapter.order_id_map)

    broker.release.set()
    await _until(lambda: _rows(adapter, "late_result"))

    assert adapter.live_orders == {}, "the Trade is not kept"
    assert dict(adapter.order_id_map) == map_before, "no broker id is bound"
    assert set(adapter.get_phantom_candidates()) == phantoms_before
    assert sink.qsize() == released_before, "the late answer releases no pending slot"
    adapter.client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_a_result_inside_the_timeout_is_not_a_late_result(tmp_config):
    adapter = _adapter(tmp_config, place_order=MagicMock(return_value={"order": {"ordno": "A1"}}))
    adapter._api_timeout_s = 2.0

    ok = await adapter._dispatch_to_api(_command())
    # Let any stray call_soon_threadsafe callback run before asserting an absence.
    await asyncio.sleep(0)

    assert ok is True
    assert _rows(adapter, "late_result") == []
    adapter.metrics.place_order_late_result_total.labels.assert_not_called()


@pytest.mark.asyncio
async def test_a_timed_out_cancel_is_not_reported_as_a_late_place_result(tmp_config):
    adapter = _adapter(tmp_config, place_order=MagicMock())
    adapter._api_timeout_s = 0.05
    release = threading.Event()
    answered = threading.Event()

    def _slow_cancel(*_a: Any, **_kw: Any) -> None:
        release.wait(timeout=5.0)
        answered.set()

    await adapter._call_api("cancel_order", _slow_cancel, intent=_intent())
    release.set()
    await asyncio.get_running_loop().run_in_executor(None, answered.wait, 5.0)
    await asyncio.sleep(0)

    assert _rows(adapter, "late_result") == []


# --------------------------------------------------------------------------- #
# _run_blocking_call: the three kinds                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_blocking_call_reports_a_result_that_arrives_after_its_waiter_gave_up(tmp_config):
    adapter = _adapter(tmp_config, place_order=MagicMock())
    seen: list[tuple[str, Any]] = []
    release = threading.Event()

    def _fn() -> str:
        release.wait(timeout=5.0)
        return "TRADE"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adapter._run_blocking_call(_fn, _on_late=lambda kind, v: seen.append((kind, v))), 0.05)
    release.set()
    await _until(lambda: seen)

    assert seen == [("trade", "TRADE")]


@pytest.mark.asyncio
async def test_run_blocking_call_reports_a_call_that_was_cancelled_before_it_reached_the_sdk(tmp_config):
    adapter = _adapter(tmp_config, place_order=MagicMock())
    seen: list[tuple[str, Any]] = []
    release = threading.Event()

    def _fn() -> None:
        release.wait(timeout=5.0)
        raise adapter_module._TimeoutCancelled()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adapter._run_blocking_call(_fn, _on_late=lambda kind, v: seen.append((kind, v))), 0.05)
    release.set()
    await _until(lambda: seen)

    assert [k for k, _ in seen] == ["cancelled_before_send"]


@pytest.mark.asyncio
async def test_run_blocking_call_reports_nothing_when_the_waiter_got_the_answer(tmp_config):
    adapter = _adapter(tmp_config, place_order=MagicMock())
    seen: list[tuple[str, Any]] = []

    result = await adapter._run_blocking_call(lambda: 5, _on_late=lambda kind, v: seen.append((kind, v)))
    await asyncio.sleep(0)

    assert result == 5
    assert seen == []


@pytest.mark.asyncio
async def test_a_failing_late_callback_does_not_escape_into_the_loop(tmp_config):
    adapter = _adapter(tmp_config, place_order=MagicMock())
    loop = asyncio.get_running_loop()
    escaped: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: escaped.append(context))
    release = threading.Event()
    done = threading.Event()

    def _fn() -> str:
        release.wait(timeout=5.0)
        return "TRADE"

    def _boom(kind: str, value: Any) -> None:
        done.set()
        raise RuntimeError("callback bug")

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter._run_blocking_call(_fn, _on_late=_boom), 0.05)
        release.set()
        await loop.run_in_executor(None, done.wait, 5.0)
        await asyncio.sleep(0)  # an unhandled callback exception reaches the handler here
    finally:
        loop.set_exception_handler(None)

    assert done.is_set()
    assert escaped == []


@pytest.mark.asyncio
async def test_a_trade_whose_ids_cannot_be_read_still_gets_its_report(tmp_config):
    class _Hostile:
        @property
        def order(self) -> Any:
            raise RuntimeError("SDK object refuses attribute access")

        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("SDK object refuses attribute access")

    broker = _LateBroker(_Hostile())
    adapter = _adapter(tmp_config, place_order=broker)

    await adapter._dispatch_to_api(_command())
    broker.release.set()
    await _until(lambda: _rows(adapter, "late_result"))

    assert "has_ids=False" in _rows(adapter, "late_result")[0]["details"]
    adapter.metrics.place_order_late_result_total.labels.assert_called_with(kind="trade")
