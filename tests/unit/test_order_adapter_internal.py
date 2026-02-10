import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, Side, StormGuardState, TIF
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter


def _make_adapter(tmp_path: Path) -> OrderAdapter:
    cfg_path = tmp_path / "order_cfg.yaml"
    cfg_path.write_text("{}\n")
    return OrderAdapter(str(cfg_path), asyncio.Queue(), MagicMock())


def _make_cmd(
    intent_type: IntentType,
    *,
    intent_id: int = 1,
    target_order_id: str | None = None,
    created_ns: int = 0,
) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id="strat",
        symbol="TXF",
        intent_type=intent_type,
        side=Side.BUY,
        price=100,
        qty=1,
        tif=TIF.LIMIT,
        target_order_id=target_order_id,
        trace_id="trace",
    )
    return OrderCommand(
        cmd_id=intent_id,
        intent=intent,
        deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=created_ns,
    )


def test_coalesce_key_variants(tmp_path):
    adapter = _make_adapter(tmp_path)
    new_cmd = _make_cmd(IntentType.NEW)
    cancel_cmd = _make_cmd(IntentType.CANCEL, target_order_id="OID")
    amend_cmd = _make_cmd(IntentType.AMEND, target_order_id="OID")

    assert adapter._coalesce_key(new_cmd) == ("new", "strat", "TXF")
    assert adapter._coalesce_key(cancel_cmd) == ("cancel", "strat", "OID")
    assert adapter._coalesce_key(amend_cmd) == ("amend", "strat", "OID")


def test_store_pending_coalesces_cancel_over_amend(tmp_path):
    adapter = _make_adapter(tmp_path)
    amend_cmd = _make_cmd(IntentType.AMEND, target_order_id="OID", intent_id=2)
    cancel_cmd = _make_cmd(IntentType.CANCEL, target_order_id="OID", intent_id=3)

    adapter._api_pending[("amend", "strat", "OID")] = amend_cmd
    adapter._store_pending(cancel_cmd)

    assert ("amend", "strat", "OID") not in adapter._api_pending
    assert ("cancel", "strat", "OID") in adapter._api_pending


def test_store_pending_skips_amend_when_cancel_pending(tmp_path):
    adapter = _make_adapter(tmp_path)
    cancel_cmd = _make_cmd(IntentType.CANCEL, target_order_id="OID", intent_id=4)
    amend_cmd = _make_cmd(IntentType.AMEND, target_order_id="OID", intent_id=5)

    adapter._api_pending[("cancel", "strat", "OID")] = cancel_cmd
    adapter._store_pending(amend_cmd)

    assert adapter._api_pending.get(("cancel", "strat", "OID")) is cancel_cmd
    assert ("amend", "strat", "OID") not in adapter._api_pending


def test_record_queue_latency_calls_recorder(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path)
    adapter.latency = MagicMock()
    monkeypatch.setattr(timebase, "now_ns", lambda: 1_000)

    cmd = _make_cmd(IntentType.NEW, created_ns=900)
    adapter._record_queue_latency(cmd)

    adapter.latency.record.assert_called_once()
    args, kwargs = adapter.latency.record.call_args
    assert args[0] == "order_queue"
    assert args[1] == 100
    assert kwargs["symbol"] == "TXF"
    assert kwargs["strategy_id"] == "strat"


def test_validate_client_by_intent(tmp_path):
    adapter = _make_adapter(tmp_path)

    class NewClient:
        def place_order(self):
            return None

        def get_exchange(self, _symbol):
            return "TSE"

    adapter.client = NewClient()
    assert adapter._validate_client(_make_cmd(IntentType.NEW).intent) is True

    class CancelClient:
        def cancel_order(self):
            return None

    adapter.client = CancelClient()
    assert adapter._validate_client(_make_cmd(IntentType.CANCEL, target_order_id="OID").intent) is True

    class AmendClient:
        def update_order(self):
            return None

    adapter.client = AmendClient()
    assert adapter._validate_client(_make_cmd(IntentType.AMEND, target_order_id="OID").intent) is True

    adapter.client = object()
    assert adapter._validate_client(_make_cmd(IntentType.NEW).intent) is False


@pytest.mark.asyncio
async def test_enqueue_api_drops_when_full(tmp_path):
    adapter = _make_adapter(tmp_path)
    adapter._api_queue = asyncio.Queue(maxsize=1)
    await adapter._api_queue.put(_make_cmd(IntentType.NEW))
    await adapter._enqueue_api(_make_cmd(IntentType.NEW, intent_id=9))
    assert adapter._api_queue.qsize() == 1


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ConnectionError("boom"), True),
        (asyncio.TimeoutError(), True),
        (RuntimeError("ECONNREFUSED"), True),
        (RuntimeError("temporarily unavailable"), True),
        (RuntimeError("permanent"), False),
    ],
)
def test_is_transient_error(tmp_path, exc, expected):
    adapter = _make_adapter(tmp_path)
    assert adapter._is_transient_error(exc) is expected
