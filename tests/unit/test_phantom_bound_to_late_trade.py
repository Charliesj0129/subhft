"""A timed-out ``place_order`` the broker accepted is taken over and cancelled.

THESHOW, R47 (SIM), 2026-10-05..06: 40 of 40 ``place_order`` calls that hit the 3 s
wrapper timeout were accepted by the broker; the Trade came back 3.1-21.4 s later and
was dropped. The pending slot was handed back after 30 s while the order sat live, so
R47 quoted again (``local_pos`` reached +2 against ``max_pos=1``), and the order's ack
was bound to the key of an ended order (``R47_MAKER_TMF:6145``, 10/6 09:35).

The Trade now gets its own key, is tracked like a normally placed order and is
cancelled; the slot comes back with the order's own terminal, once. A call the SDK
says never happened releases at once; an answer that never comes falls back to a
300 s TTL.
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
_KEY = f"{_STRATEGY}:7"
_TRADE = {"order": {"ordno": "A1B2C", "seqno": "000123"}}


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


def _adapter(tmp_config: str, *, place_order: Any, cancel_order: Any = None) -> OrderAdapter:
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.place_order = place_order
    if cancel_order is not None:
        client.cancel_order = cancel_order
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


def _intent(intent_id: int = 7, intent_type: IntentType = IntentType.NEW) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=_STRATEGY,
        symbol="TMFI6",
        price=4623_9000,
        qty=1,
        side=Side.BUY,
        intent_type=intent_type,
        tif=TIF.LIMIT,
    )


def _command(intent_id: int = 7, intent_type: IntentType = IntentType.NEW) -> OrderCommand:
    return OrderCommand(
        cmd_id=intent_id,
        intent=_intent(intent_id, intent_type),
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


def _rows(adapter: OrderAdapter, event: str) -> list[dict[str, Any]]:
    return [c.args[0] for c in adapter._audit_writer.log_order.call_args_list if c.args[0].get("event") == event]


def _outcomes(adapter: OrderAdapter) -> list[str]:
    return [c.kwargs["outcome"] for c in adapter.metrics.phantom_bound_total.labels.call_args_list]


def _feedback(adapter: OrderAdapter) -> list[Any]:
    sink = adapter._rejection_sink
    return [sink.get_nowait() for _ in range(sink.qsize())]


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
        self._outcome = outcome

    def __call__(self, *_a: Any, **_kw: Any) -> Any:
        self.release.wait(timeout=5.0)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


async def _timed_out_then_answered(adapter: OrderAdapter, broker: _LateBroker, **cmd_kw: Any) -> None:
    ok = await adapter._dispatch_to_api(_command(**cmd_kw))
    assert ok is False, "the call timed out"
    broker.release.set()


# --------------------------------------------------------------------------- #
# The Trade is taken over                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_late_trade_is_bound_tracked_and_cancelled(tmp_config):
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker)

    await _timed_out_then_answered(adapter, broker)
    await _until(lambda: _rows(adapter, "phantom_bound"))

    assert adapter.order_id_map["A1B2C"] == _KEY
    assert adapter.order_id_map["000123"] == _KEY
    assert adapter.live_orders[_KEY] is _TRADE
    adapter.client.cancel_order.assert_called_once_with(_TRADE)
    assert _outcomes(adapter) == ["cancel_sent"]
    (row,) = _rows(adapter, "phantom_bound")
    assert row["order_key"] == _KEY
    assert "outcome=cancel_sent" in row["details"]
    assert "slot_held=True" in row["details"]


@pytest.mark.asyncio
async def test_a_late_trade_overwrites_a_stale_binding_of_the_same_broker_id(tmp_config):
    """THESHOW 10/6 09:35: the ack of the timed-out order was bound to R47_MAKER_TMF:6145,
    an order that had ended hours earlier. Registering the Trade's ids repoints them."""
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker)
    adapter.order_id_map["000123"] = f"{_STRATEGY}:6145"

    await _timed_out_then_answered(adapter, broker)
    await _until(lambda: _rows(adapter, "phantom_bound"))

    assert adapter.order_id_map["000123"] == _KEY


@pytest.mark.asyncio
async def test_a_bound_order_keeps_its_slot_until_its_own_terminal(tmp_config):
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker)

    await _timed_out_then_answered(adapter, broker)
    await _until(lambda: _rows(adapter, "phantom_bound"))
    released_by_ttl = await adapter.release_stale_phantom_pendings(ttl_s=0.0)

    assert released_by_ttl == 0, "the phantom record was taken over, the TTL sweep has nothing to release"
    assert adapter.get_phantom_candidates() == frozenset()
    assert [f for f in _feedback(adapter) if not f.was_approved] == [], "no slot is handed back yet"
    assert _KEY in adapter._live_order_intents, "if no terminal ever comes the live-order sweep releases it once"

    await adapter.on_terminal_state(_STRATEGY, "A1B2C")  # the CANCELLED ack, resolved through the bound id

    assert _KEY not in adapter.live_orders


@pytest.mark.asyncio
async def test_a_late_trade_after_the_ttl_release_is_still_cancelled_but_releases_nothing_more(tmp_config):
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker)
    adapter._phantom_recovery_ttl_s = 0.0
    ok = await adapter._dispatch_to_api(_command())
    assert ok is False
    _feedback(adapter)  # the phantom_pending=True feedback of the dispatch itself
    assert await adapter.release_stale_phantom_pendings() == 1
    (released,) = _feedback(adapter)
    assert released.was_approved is False

    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))

    adapter.client.cancel_order.assert_called_once_with(_TRADE)
    assert adapter.order_finished(_KEY), "the TTL release is on record: the terminal releases 0"
    assert _KEY not in adapter._live_order_intents, "a second release by the live-order sweep is not armed"
    assert "slot_held=False" in _rows(adapter, "phantom_bound")[0]["details"]
    adapter._live_orders_last_sweep_s = float("-inf")
    adapter._live_orders_ttl_s = 0.0
    await adapter.sweep_stale_live_orders()
    assert _feedback(adapter) == []


@pytest.mark.asyncio
async def test_a_late_trade_that_already_filled_is_not_cancelled(tmp_config):
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker)
    ok = await adapter._dispatch_to_api(_command())
    assert ok is False
    adapter._record_fill_ledger(_KEY, 1)  # the deal reached the platform before the Trade did

    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))

    adapter.client.cancel_order.assert_not_called()
    assert _outcomes(adapter) == ["skipped_filled"]
    assert _KEY not in adapter.live_orders


@pytest.mark.asyncio
async def test_a_late_force_flat_trade_is_never_cancelled(tmp_config):
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker)
    adapter._platform_net_position_for_symbol = MagicMock(return_value=1)

    await _timed_out_then_answered(adapter, broker, intent_type=IntentType.FORCE_FLAT)
    await _until(lambda: _rows(adapter, "late_result"))
    await asyncio.sleep(0.05)

    adapter.client.cancel_order.assert_not_called()
    assert _rows(adapter, "phantom_bound") == []


@pytest.mark.asyncio
async def test_a_late_trade_without_ids_is_left_to_the_ttl(tmp_config):
    broker = _LateBroker({})
    adapter = _adapter(tmp_config, place_order=broker)

    await _timed_out_then_answered(adapter, broker)
    await _until(lambda: _rows(adapter, "late_result"))
    await asyncio.sleep(0.05)

    adapter.client.cancel_order.assert_not_called()
    assert _outcomes(adapter) == ["kept_for_ttl"]
    assert adapter.get_phantom_candidates() == frozenset({_KEY})


# --------------------------------------------------------------------------- #
# How the cancel can end                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failed_cancel_is_watched_and_retried_once(tmp_config):
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker, cancel_order=MagicMock(side_effect=RuntimeError("rejected")))

    await _timed_out_then_answered(adapter, broker)
    await _until(lambda: _rows(adapter, "phantom_bound"))

    assert _outcomes(adapter) == ["cancel_failed"]
    assert _KEY in adapter._cancel_watch, "check_unconfirmed_cancels re-sends it once"
    assert adapter._is_cancel_inflight(_KEY) is False
    assert adapter.live_orders[_KEY] is _TRADE, "a failed cancel leaves the order live"


@pytest.mark.asyncio
async def test_a_timed_out_cancel_is_watched_on_the_short_window_and_not_repeated(tmp_config):
    broker = _LateBroker(_TRADE)
    release_cancel = threading.Event()
    calls: list[Any] = []

    def _slow_cancel(trade: Any) -> None:
        calls.append(trade)
        release_cancel.wait(timeout=5.0)

    adapter = _adapter(tmp_config, place_order=broker, cancel_order=_slow_cancel)

    await _timed_out_then_answered(adapter, broker)
    await _until(lambda: _rows(adapter, "phantom_bound"))
    release_cancel.set()

    assert _outcomes(adapter) == ["cancel_timeout"]
    assert len(calls) == 1, "a timed-out cancel is not sent again blind"
    assert adapter._cancel_watch[_KEY].confirm_s == adapter._cancel_timeout_confirm_s
    assert adapter._is_cancel_inflight(_KEY)


@pytest.mark.asyncio
async def test_a_second_cancel_for_the_bound_order_is_a_no_op(tmp_config):
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker)

    await _timed_out_then_answered(adapter, broker)
    await _until(lambda: _rows(adapter, "phantom_bound"))
    strategy_cancel = OrderCommand(
        cmd_id=99,
        intent=OrderIntent(
            intent_id=99,
            strategy_id=_STRATEGY,
            symbol="TMFI6",
            price=0,
            qty=1,
            side=Side.BUY,
            intent_type=IntentType.CANCEL,
            target_order_id=_KEY,
        ),
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )
    await adapter._dispatch_to_api(strategy_cancel)

    adapter.client.cancel_order.assert_called_once()
    assert _rows(adapter, "cancel_no_op_already_inflight")


# --------------------------------------------------------------------------- #
# An answer that says no order exists                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_sdk_error_that_proves_nothing_was_sent_hands_the_slot_back_at_once(tmp_config):
    broker = _LateBroker(RuntimeError("Session error SessionNotEstablished"))
    adapter = _adapter(tmp_config, place_order=broker)
    ok = await adapter._dispatch_to_api(_command())
    assert ok is False
    _feedback(adapter)

    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_released"))

    (released,) = _feedback(adapter)
    assert released.was_approved is False
    assert released.reason_code == "place_order_not_sent"
    assert adapter.get_phantom_candidates() == frozenset()
    assert adapter.order_finished(_KEY)
    assert _outcomes(adapter) == ["released_not_sent"]
    adapter.client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_an_sdk_timeout_error_keeps_the_slot_for_the_ttl(tmp_config):
    """A timeout INSIDE the SDK does not prove the order was not accepted."""
    broker = _LateBroker(RuntimeError("request timed out waiting for reply"))
    adapter = _adapter(tmp_config, place_order=broker)
    ok = await adapter._dispatch_to_api(_command())
    assert ok is False
    _feedback(adapter)

    broker.release.set()
    await _until(lambda: _outcomes(adapter))

    assert _outcomes(adapter) == ["kept_for_ttl"]
    assert _feedback(adapter) == []
    assert adapter.get_phantom_candidates() == frozenset({_KEY})


@pytest.mark.asyncio
async def test_a_call_stopped_before_it_reached_the_sdk_hands_the_slot_back(tmp_config):
    adapter = _adapter(tmp_config, place_order=MagicMock())
    ok = await adapter._dispatch_to_api(_command())  # registers nothing: the call answered
    assert ok is True
    # Reproduce the timed-out dispatch's state, then deliver the guard's answer.
    with adapter._phantom_lock:
        adapter._register_phantom(_intent(8))
    _feedback(adapter)
    reporter = adapter._late_place_reporter(_intent(8), 0)
    assert reporter is not None

    reporter("cancelled_before_send", adapter_module._TimeoutCancelled())

    (released,) = _feedback(adapter)
    assert released.was_approved is False
    assert adapter.get_phantom_candidates() == frozenset()


# --------------------------------------------------------------------------- #
# The fallback                                                                 #
# --------------------------------------------------------------------------- #


def test_the_phantom_fallback_ttl_matches_the_live_order_ttl(tmp_config):
    adapter = _adapter(tmp_config, place_order=MagicMock())

    assert adapter._phantom_recovery_ttl_s == 300.0
    assert adapter._phantom_recovery_ttl_s >= adapter._live_orders_ttl_s


@pytest.mark.asyncio
async def test_a_fill_claimed_before_the_trade_arrives_stops_the_bind_from_cancelling(tmp_config):
    """A fill that beat the Trade is attributed through the phantom record; the order is
    done, so the late Trade must not be cancelled (a failed cancel feeds the breakers)."""
    broker = _LateBroker(_TRADE)
    adapter = _adapter(tmp_config, place_order=broker)
    ok = await adapter._dispatch_to_api(_command())
    assert ok is False
    fill = MagicMock(symbol="TMFI6", side=Side.BUY, qty=1)

    assert adapter.resolve_phantom_fill(fill) == _STRATEGY
    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))

    adapter.client.cancel_order.assert_not_called()
    assert _outcomes(adapter) == ["skipped_filled"]
    assert "slot_held=False" in _rows(adapter, "phantom_bound")[0]["details"]
    assert _KEY not in adapter._live_order_intents, "the fill already gave the slot back"
