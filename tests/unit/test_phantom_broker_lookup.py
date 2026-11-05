"""A TTL-expired phantom gives its slot back only when the broker has no such order.

THESHOW 2026-11-05: ``place_order`` R47:33723 hit the SDK's 30 s timeout with no
Trade, so the phantom stayed unbound; the 300 s TTL handed the slot back while
the order sat at the broker as 1B2839, R47 quoted again, and the old order
filled 6 min later: R47 -2 against ``max_pos=1``. The TTL sweep now asks the
broker for its working orders first.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.order.adapter import OrderAdapter, _PhantomEntry

KEY = "s1:42"
TOKEN = "00002A"


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text("rate_limits:\n  shioaji_soft_cap: 180\n  shioaji_hard_cap: 250\n  window_seconds: 10\n")
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


class _ListingClient:
    """A broker client that can list working orders (the capability is on the class)."""

    def __init__(self, trades: list[Any] | None = None, error: Exception | None = None) -> None:
        self.trades = trades or []
        self.error = error
        self.calls = 0
        self.mode = "simulation"
        self.activate_ca = False

    def list_open_orders(self) -> list[Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.trades)

    def get_exchange(self, symbol: str) -> str:
        return "TAIFEX"


class _Codec:
    def scale(self, symbol: str, price: float) -> int:
        return int(Decimal(str(price)) * 10000)


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id=42,
        strategy_id="s1",
        symbol="TMFK6",
        price=27_850_0000,
        qty=1,
        side=Side.SELL,
        intent_type=IntentType.NEW,
    )


def _trade(*, custom_field: str | None, ordno: str = "1B2839", price: float = 27850.0, action: str = "Sell") -> Any:
    return SimpleNamespace(
        contract=SimpleNamespace(code="TMFK6"),
        order=SimpleNamespace(
            id="x1", seqno="s1", ordno=ordno, action=action, price=price, quantity=1, custom_field=custom_field
        ),
        status=SimpleNamespace(id="x1", status="Submitted"),
    )


def _adapter(tmp_config: str, client: Any) -> tuple[OrderAdapter, asyncio.Queue]:
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=8), broker_client=client)
    adapter.price_codec = _Codec()
    adapter._resolve_broker_contract_code = lambda intent: intent.symbol  # type: ignore[method-assign]  # test seam
    sink: asyncio.Queue = asyncio.Queue(maxsize=16)
    adapter.set_rejection_sink(sink)
    intent = _intent()
    aged = time.monotonic() - 999.0
    adapter._phantom_records[KEY] = [
        _PhantomEntry(monotonic_ts=aged, symbol=intent.symbol, created_ns=0, intent=intent)
    ]
    adapter._phantom_order_keys[KEY] = (aged, intent.symbol)
    adapter._phantom_intents[KEY] = intent
    adapter.order_id_map[TOKEN] = KEY
    adapter._spawn_phantom_bind = MagicMock()  # type: ignore[method-assign]  # record the decision only
    return adapter, sink


async def _sweep(adapter: OrderAdapter) -> int:
    released = await adapter.release_stale_phantom_pendings(ttl_s=30.0)
    task = adapter._phantom_lookup_task
    if task is not None:
        await task
    return released


def _released_feedbacks(sink: asyncio.Queue) -> list[Any]:
    out = []
    while not sink.empty():
        fb = sink.get_nowait()
        if not fb.was_approved:
            out.append(fb)
    return out


@pytest.mark.asyncio
async def test_expired_phantom_found_open_at_broker_is_bound_not_released(tmp_config):
    trade = _trade(custom_field=TOKEN)
    adapter, sink = _adapter(tmp_config, _ListingClient([trade]))

    await _sweep(adapter)

    assert _released_feedbacks(sink) == []  # the slot stays with the order
    adapter._spawn_phantom_bind.assert_called_once()
    assert adapter._spawn_phantom_bind.call_args.args[1] is trade
    assert KEY in adapter._phantom_records  # the bind takes the record, not the sweep


@pytest.mark.asyncio
async def test_expired_phantom_absent_at_broker_is_released(tmp_config):
    client = _ListingClient([])
    adapter, sink = _adapter(tmp_config, client)

    await _sweep(adapter)

    assert client.calls == 1
    assert len(_released_feedbacks(sink)) == 1
    assert KEY not in adapter._phantom_records
    adapter._spawn_phantom_bind.assert_not_called()


@pytest.mark.asyncio
async def test_expired_phantom_kept_when_broker_lookup_fails_until_max_hold(tmp_config):
    adapter, sink = _adapter(tmp_config, _ListingClient(error=TimeoutError("update_status timed out")))
    adapter._phantom_lookup_max_hold_s = 3600.0

    await _sweep(adapter)
    assert _released_feedbacks(sink) == []
    assert KEY in adapter._phantom_records

    adapter._phantom_lookup_max_hold_s = 0.0  # held past the bound: the old release applies
    await _sweep(adapter)
    assert len(_released_feedbacks(sink)) == 1
    assert KEY not in adapter._phantom_records


@pytest.mark.asyncio
async def test_expired_phantom_matched_by_order_fields_when_sdk_omits_custom_field(tmp_config):
    trade = _trade(custom_field=None)
    other_price = _trade(custom_field=None, ordno="1B2900", price=27851.0)
    adapter, sink = _adapter(tmp_config, _ListingClient([other_price, trade]))

    await _sweep(adapter)

    assert _released_feedbacks(sink) == []
    assert adapter._spawn_phantom_bind.call_args.args[1] is trade


@pytest.mark.asyncio
async def test_expired_phantom_held_when_two_broker_orders_fit(tmp_config):
    adapter, sink = _adapter(
        tmp_config, _ListingClient([_trade(custom_field=None), _trade(custom_field=None, ordno="1B283B")])
    )

    await _sweep(adapter)

    assert _released_feedbacks(sink) == []
    adapter._spawn_phantom_bind.assert_not_called()
    assert KEY in adapter._phantom_records


@pytest.mark.asyncio
async def test_expired_phantom_ignores_order_bound_to_another_platform_order(tmp_config):
    adapter, sink = _adapter(tmp_config, _ListingClient([_trade(custom_field=None)]))
    adapter.order_id_map["1B2839"] = "s1:7"  # a tracked order, not this phantom

    await _sweep(adapter)

    adapter._spawn_phantom_bind.assert_not_called()
    assert len(_released_feedbacks(sink)) == 1


@pytest.mark.asyncio
async def test_expired_phantom_other_token_is_not_this_order(tmp_config):
    adapter, sink = _adapter(tmp_config, _ListingClient([_trade(custom_field="00002B")]))

    await _sweep(adapter)

    adapter._spawn_phantom_bind.assert_not_called()
    assert len(_released_feedbacks(sink)) == 1


@pytest.mark.asyncio
async def test_client_without_order_listing_keeps_unconditional_ttl_release(tmp_config):
    client = MagicMock()  # answers to any attribute, defines no list_open_orders
    adapter, sink = _adapter(tmp_config, client)

    released = await adapter.release_stale_phantom_pendings(ttl_s=30.0)

    assert released == 1
    assert adapter._phantom_lookup_task is None
    assert len(_released_feedbacks(sink)) == 1


def test_registry_creates_each_broker_lookup_outcome_at_zero() -> None:
    from hft_platform.observability.metrics import BROKER_LOOKUP_PHANTOM_OUTCOMES, MetricsRegistry

    MetricsRegistry._instance = None
    try:
        counter = MetricsRegistry.get().phantom_bound_total
        samples = {
            s.labels["outcome"]: s.value
            for metric in counter.collect()
            for s in metric.samples
            if s.name.endswith("_total")
        }
    finally:
        MetricsRegistry._instance = None

    assert {o: samples.get(o) for o in BROKER_LOOKUP_PHANTOM_OUTCOMES} == dict.fromkeys(
        BROKER_LOOKUP_PHANTOM_OUTCOMES, 0.0
    )


@pytest.mark.asyncio
async def test_expired_phantom_never_takes_a_manual_order_when_sdk_echoes_tokens(tmp_config):
    """Another platform order shows the SDK echoes custom_field; the token-less twin is manual."""
    manual = _trade(custom_field=None)
    platform_other = _trade(custom_field="000010", ordno="1B3000", price=27900.0)
    adapter, sink = _adapter(tmp_config, _ListingClient([manual, platform_other]))

    await _sweep(adapter)

    adapter._spawn_phantom_bind.assert_not_called()
    assert len(_released_feedbacks(sink)) == 1


@pytest.mark.asyncio
async def test_broker_lookup_crash_is_logged_and_keeps_the_slot(tmp_config):
    adapter, sink = _adapter(tmp_config, _ListingClient([_trade(custom_field=TOKEN)]))
    adapter._match_open_order = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]  # force a crash

    await _sweep(adapter)

    assert _released_feedbacks(sink) == []
    assert KEY in adapter._phantom_records
