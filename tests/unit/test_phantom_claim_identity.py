"""A phantom's claim of an unknown-ordno fill is tentative until its own Trade confirms it.

THESHOW 2026-11-12 03:34Z (R47, SIM): ``place_order`` R47_MAKER_TMF:6816 BUY timed
out and became a phantom. At 03:34:03.691Z a BUY fill with an ordno the platform did
not know arrived and was given to 6816 by symbol and side; the record was popped and
the fill noted in 6816's ledger. 6816's Trade came back later with ANOTHER ordno and
``order_datetime`` 03:34:11.946Z -- eight seconds after the fill it had "taken" -- and
``_bind_late_trade`` saw the order "filled" and skipped its cancel. R47 sent 6818 and
reached +2 against max_pos=1 at 03:34:12.104Z for ~16 s.

Now the claim books the fill to the strategy but keeps the record; the phantom's Trade
confirms it (its ids contain the fill's ordno) or exposes it, and an exposed claim
never stops the cancel of the working order.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter
from hft_platform.risk.storm_guard import StormGuardState

_STRATEGY = "R47_MAKER_TMF"
_KEY = f"{_STRATEGY}:6816"
_UTC = dt.timezone.utc
# 03:34:03.691Z; whole seconds from datetime, the milliseconds added as integers
_FILL_TS_NS = int(dt.datetime(2026, 11, 12, 3, 34, 3, tzinfo=_UTC).timestamp()) * 10**9 + 691_000_000
# 6816's broker stamp: naive Asia/Taipei, 11:34:11.946 == 03:34:11.946Z
_ORDER_DT_TAIPEI = dt.datetime(2026, 11, 12, 11, 34, 11, 946000)


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


class _LateBroker:
    """A place_order that answers only when released -- after the wrapper timed out."""

    def __init__(self, outcome: Any) -> None:
        self.release = threading.Event()
        self._outcome = outcome

    def __call__(self, *_a: Any, **_kw: Any) -> Any:
        self.release.wait(timeout=5.0)
        return self._outcome


def _adapter(tmp_config: str, broker: _LateBroker) -> OrderAdapter:
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.place_order = broker
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=64), broker_client=client)
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


def _command(intent_id: int = 6816) -> OrderCommand:
    return OrderCommand(
        cmd_id=intent_id,
        intent=OrderIntent(
            intent_id=intent_id,
            strategy_id=_STRATEGY,
            symbol="TMFL6",
            price=27_850_0000,
            qty=1,
            side=Side.BUY,
            intent_type=IntentType.NEW,
            tif=TIF.LIMIT,
        ),
        deadline_ns=timebase.now_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )


def _fill(ordno: str = "0A11FF", ts_ns: int = _FILL_TS_NS) -> Any:
    return SimpleNamespace(symbol="TMFL6", side=Side.BUY, qty=1, order_id=ordno, match_ts_ns=ts_ns)


def _trade(ordno: str, order_datetime: Any = _ORDER_DT_TAIPEI) -> Any:
    return SimpleNamespace(
        contract=SimpleNamespace(code="TMFL6"),
        order=SimpleNamespace(id="b7c1", seqno="0007A1", ordno=ordno, action="Buy", price=27_850.0, quantity=1),
        status=SimpleNamespace(id="b7c1", status="Submitted", order_datetime=order_datetime),
    )


def _rows(adapter: OrderAdapter, event: str) -> list[dict[str, Any]]:
    return [c.args[0] for c in adapter._audit_writer.log_order.call_args_list if c.args[0].get("event") == event]


def _bound(adapter: OrderAdapter) -> list[str]:
    return [c.kwargs["outcome"] for c in adapter.metrics.phantom_bound_total.labels.call_args_list]


def _claims(adapter: OrderAdapter) -> list[str]:
    return [c.kwargs["outcome"] for c in adapter.metrics.phantom_claim_total.labels.call_args_list]


def _feedback(adapter: OrderAdapter) -> list[Any]:
    sink = adapter._rejection_sink
    return [sink.get_nowait() for _ in range(sink.qsize())]


async def _until(predicate: Any, timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


# --------------------------------------------------------------------------- #
# The 11-12 03:34 replay                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_misattributed_claim_cancels_the_working_order_0334(tmp_config):
    broker = _LateBroker(_trade("0A1234"))  # 6816's own ordno is not the fill's
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False  # timed out: phantom 6816
    _feedback(adapter)

    assert adapter.resolve_phantom_fill(_fill()) == _STRATEGY, "the fill is still booked to R47"
    assert adapter.get_phantom_candidates() == frozenset({_KEY}), "the record is kept"
    assert adapter.filled_qty_for(_KEY) == 0, "nothing noted in 6816's ledger before it is confirmed"

    with patch("hft_platform.order.adapter.logger") as log:
        broker.release.set()
        await _until(lambda: _rows(adapter, "phantom_bound"))

    adapter.client.cancel_order.assert_called_once()
    assert adapter.client.cancel_order.call_args.args[0] is broker._outcome
    assert "skipped_filled" not in _bound(adapter)
    assert _bound(adapter) == ["cancel_sent"]
    assert _claims(adapter) == ["tentative", "misattributed"]
    critical = [c for c in log.critical.call_args_list if c.args[0] == "claim_misattributed"]
    assert len(critical) == 1
    assert critical[0].kwargs["order_key"] == _KEY
    assert adapter.live_orders[_KEY] is broker._outcome, "bound and tracked as a working order"
    assert adapter.order_id_map["0A1234"] == _KEY
    assert _KEY not in adapter._live_order_intents, "the claimed fill already freed the slot; no second release"
    assert adapter.order_finished(_KEY), "6816's own terminal must release nothing more"
    assert "fill_claimed_after_expiry" not in _bound(adapter)


@pytest.mark.asyncio
async def test_claim_with_matching_ordno_but_later_order_datetime_is_misattributed(tmp_config):
    """The broker stamped the order after the fill it was given: it cannot be that fill's order."""
    broker = _LateBroker(_trade("0A11FF"))
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False
    adapter.resolve_phantom_fill(_fill("0A11FF"))

    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))

    adapter.client.cancel_order.assert_called_once()
    assert _claims(adapter) == ["tentative", "misattributed"]


@pytest.mark.asyncio
async def test_claim_of_a_fill_without_ordno_is_misattributed(tmp_config):
    broker = _LateBroker(_trade("0A1234", order_datetime=dt.datetime(2026, 11, 12, 11, 34, 0)))
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False
    adapter.resolve_phantom_fill(_fill(ordno=""))

    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))

    adapter.client.cancel_order.assert_called_once()
    assert "skipped_filled" not in _bound(adapter)
    assert _claims(adapter) == ["tentative", "misattributed"]


# --------------------------------------------------------------------------- #
# Identity match                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_claim_confirmed_by_identity_skips_the_cancel(tmp_config):
    broker = _LateBroker(_trade("0A11FF", order_datetime=dt.datetime(2026, 11, 12, 11, 34, 2, 100000)))
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False
    adapter.resolve_phantom_fill(_fill("0A11FF"))

    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))

    adapter.client.cancel_order.assert_not_called()
    assert _bound(adapter) == ["skipped_filled"]
    assert _claims(adapter) == ["tentative", "confirmed"]
    assert adapter.filled_qty_for(_KEY) == 1, "the confirmed claim is noted in the ledger"
    assert "fill_claimed_after_expiry" not in _bound(adapter), "a live phantom's confirmed claim is resolved"
    assert adapter.get_phantom_candidates() == frozenset()


@pytest.mark.asyncio
async def test_expired_ledger_claim_pages_once_at_claim_not_again_on_confirm(tmp_config):
    broker = _LateBroker(_trade("0A11FF", order_datetime=dt.datetime(2026, 11, 12, 11, 34, 2)))
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False
    assert await adapter.release_stale_phantom_pendings(ttl_s=0.0) == 1  # TTL ran out, no lister

    assert adapter.resolve_phantom_fill(_fill("0A11FF")) == _STRATEGY
    assert _bound(adapter) == ["fill_claimed_after_expiry"], "the released slot's fill pages at claim time"

    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))

    assert _bound(adapter).count("fill_claimed_after_expiry") == 1, "not counted again on confirmation"
    assert _claims(adapter) == ["tentative", "confirmed"]
    adapter.client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_expired_ledger_claim_with_other_ordno_pages_at_claim_and_cancels(tmp_config):
    broker = _LateBroker(_trade("0A1234"))
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False
    assert await adapter.release_stale_phantom_pendings(ttl_s=0.0) == 1

    with patch("hft_platform.order.adapter.logger") as log:
        assert adapter.resolve_phantom_fill(_fill("0A11FF")) == _STRATEGY
        broker.release.set()
        await _until(lambda: _rows(adapter, "phantom_bound"))

    assert _bound(adapter).count("fill_claimed_after_expiry") == 1
    assert _claims(adapter) == ["tentative", "misattributed"]
    adapter.client.cancel_order.assert_called_once()
    assert [c.args[0] for c in log.critical.call_args_list] == ["claim_misattributed"]


# --------------------------------------------------------------------------- #
# No Trade at all                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_claimed_phantom_at_ttl_keeps_its_claim_for_a_late_trade_without_a_second_release(tmp_config):
    broker = _LateBroker(_trade("0A1234"))
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False
    _feedback(adapter)
    adapter.resolve_phantom_fill(_fill())

    with patch("hft_platform.order.adapter.logger") as log:
        released = await adapter.release_stale_phantom_pendings(ttl_s=0.0)
    broker.release.set()

    assert released == 0, "the fill already gave the slot back"
    assert _feedback(adapter) == []
    assert adapter.get_phantom_candidates() == frozenset()
    assert _claims(adapter) == ["tentative"], "still unsettled: the claim moved to the expired-claim ledger"
    assert _KEY in adapter._get_expired_claims()
    assert any(c.args[0] == "phantom_claim_kept_after_ttl" for c in log.warning.call_args_list)
    adapter.metrics.phantom_recovery_releases_total.inc.assert_not_called()
    assert len(adapter._get_expired_phantoms()) == 0, "a claimed record cannot claim a second fill"


async def _claimed_then_ttl_then_trade(tmp_config: str, trade: Any) -> OrderAdapter:
    """6816 claims a fill, its TTL runs out with no Trade (record dropped), then the
    Trade comes back late -- after the 300 s TTL in production (ttl 0 here)."""
    broker = _LateBroker(trade)
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False
    _feedback(adapter)
    assert adapter.resolve_phantom_fill(_fill("0A11FF")) == _STRATEGY
    assert await adapter.release_stale_phantom_pendings(ttl_s=0.0) == 0
    assert adapter.get_phantom_candidates() == frozenset()
    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))
    return adapter


@pytest.mark.asyncio
async def test_trade_after_ttl_confirms_a_dropped_claim_and_skips_the_cancel(tmp_config):
    adapter = await _claimed_then_ttl_then_trade(
        tmp_config, _trade("0A11FF", order_datetime=dt.datetime(2026, 11, 12, 11, 34, 2))
    )

    adapter.client.cancel_order.assert_not_called()
    assert _bound(adapter) == ["skipped_filled"]
    assert _claims(adapter) == ["tentative", "confirmed"]
    assert "fill_claimed_after_expiry" not in _bound(adapter), "a live phantom's claim never paged"


@pytest.mark.asyncio
async def test_trade_after_ttl_refutes_a_dropped_claim_and_cancels_without_a_second_release(tmp_config):
    adapter = await _claimed_then_ttl_then_trade(tmp_config, _trade("0A1234"))

    adapter.client.cancel_order.assert_called_once()
    assert _claims(adapter) == ["tentative", "misattributed"]
    assert adapter.order_finished(_KEY), "its terminal must not free R47's slot a second time"
    assert _KEY not in adapter._live_order_intents
    assert _feedback(adapter) == []


def test_a_claimed_record_does_not_claim_a_second_fill(tmp_config):
    adapter = _adapter(tmp_config, _LateBroker(None))
    with adapter._phantom_lock:
        adapter._register_phantom(_command().intent)

    assert adapter.resolve_phantom_fill(_fill()) == _STRATEGY
    assert adapter.resolve_phantom_fill(_fill("0A2222")) is None


# --------------------------------------------------------------------------- #
# order_datetime: Asia/Taipei wall time, aware on live 1.7.11 payloads        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stamp",
    [
        _ORDER_DT_TAIPEI,
        "2026-11-12 11:34:11.946000",
        _ORDER_DT_TAIPEI.replace(tzinfo=dt.timezone(dt.timedelta(hours=8))),
        # The form THESHOW's shioaji 1.7.11 engine logs carry (2026-11-12):
        # aware, with a +08:00 offset, stringified by the order-event log.
        "2026-11-12 11:34:11.946000+08:00",
        dt.datetime.fromisoformat("2026-11-12 11:34:11.946000+08:00"),
    ],
)
def test_order_datetime_naive_or_live_aware_is_read_as_asia_taipei(stamp):
    from zoneinfo import ZoneInfo

    expected = dt.datetime(2026, 11, 12, 11, 34, 11, 946000, tzinfo=ZoneInfo("Asia/Taipei"))
    utc = dt.datetime(2026, 11, 12, 3, 34, 11, tzinfo=_UTC)
    expected_ns = int(utc.timestamp()) * 10**9 + 946_000_000

    got = OrderAdapter._order_datetime_ns(SimpleNamespace(status=SimpleNamespace(order_datetime=stamp)))

    assert got == expected_ns
    assert expected.utcoffset() == dt.timedelta(hours=8)


@pytest.mark.parametrize("stamp", [None, "", "not a time"])
def test_order_datetime_unreadable_is_none(stamp):
    assert OrderAdapter._order_datetime_ns(SimpleNamespace(status=SimpleNamespace(order_datetime=stamp))) is None


def test_registry_creates_each_phantom_claim_outcome_at_zero() -> None:
    from hft_platform.observability.metrics import PHANTOM_CLAIM_OUTCOMES, MetricsRegistry

    MetricsRegistry._instance = None
    try:
        counter = MetricsRegistry.get().phantom_claim_total
        samples = {
            s.labels["outcome"]: s.value
            for metric in counter.collect()
            for s in metric.samples
            if s.name.endswith("_total")
        }
    finally:
        MetricsRegistry._instance = None

    assert set(PHANTOM_CLAIM_OUTCOMES) == {"tentative", "confirmed", "misattributed", "unverified"}
    assert {o: samples.get(o) for o in PHANTOM_CLAIM_OUTCOMES} == dict.fromkeys(PHANTOM_CLAIM_OUTCOMES, 0.0)


# --------------------------------------------------------------------------- #
# A claimed record is not matched to a listed order by its parameters          #
# --------------------------------------------------------------------------- #


class _ListingClient(MagicMock):
    """A client whose class defines ``list_open_orders`` (the capability check)."""

    def list_open_orders(self) -> list[Any]:
        return list(self.listing)


class _Codec:
    def scale(self, symbol: str, price: float) -> int:
        return int(round(price * 10000))


@pytest.mark.asyncio
async def test_claimed_phantom_is_not_matched_by_params_so_the_unclaimed_twin_gets_its_order(tmp_config):
    """P1 (older) claimed a fill and is not listed; P2 has the same parameters and its
    order O is listed open. O is P2's: binding it to P1 would 'refute' P1's claim with
    a stranger's order and release P2's slot while O works."""
    import time as _time

    from hft_platform.order.adapter import _PhantomClaim, _PhantomEntry

    listed = _trade("0B9999")  # O: same contract/side/qty/price, no custom_field
    client = _ListingClient()
    client.listing = [listed]
    client.mode = "simulation"
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=8), broker_client=client)
    adapter.price_codec = _Codec()
    adapter._resolve_broker_contract_code = lambda intent: intent.symbol  # type: ignore[method-assign]  # test seam
    adapter._broker_codec = None
    adapter._add_to_dlq = AsyncMock()
    adapter.set_rejection_sink(asyncio.Queue(maxsize=16))
    adapter._spawn_phantom_bind = MagicMock()  # type: ignore[method-assign]  # record the decision only
    p1, p2 = _command(6816).intent, _command(6817).intent
    aged = _time.monotonic() - 999.0
    claim = _PhantomClaim(ordno="0A11FF", qty=1, fill_ts_ns=_FILL_TS_NS, claimed_at=aged)
    adapter._phantom_records[f"{_STRATEGY}:6816"] = [
        _PhantomEntry(monotonic_ts=aged, symbol="TMFL6", created_ns=0, intent=p1, side=int(Side.BUY), claim=claim)
    ]
    adapter._phantom_records[f"{_STRATEGY}:6817"] = [
        _PhantomEntry(monotonic_ts=aged + 1.0, symbol="TMFL6", created_ns=0, intent=p2, side=int(Side.BUY))
    ]

    with patch("hft_platform.order.adapter.logger") as log:
        await adapter.release_stale_phantom_pendings(ttl_s=30.0)
        await adapter._phantom_lookup_task

    adapter._spawn_phantom_bind.assert_called_once()
    assert adapter._spawn_phantom_bind.call_args.args[0] is p2
    assert adapter._spawn_phantom_bind.call_args.args[1] is listed
    assert [c for c in log.critical.call_args_list if c.args[0] == "claim_misattributed"] == []
    released = [f for f in _feedback(adapter) if not f.was_approved]
    assert [f.intent_id for f in released] == [], "P2 keeps its slot; P1's claim already freed its own"
    assert f"{_STRATEGY}:6817" in adapter._phantom_records, "the bind, not the lookup, takes P2's record"


@pytest.mark.asyncio
async def test_claimed_phantom_matched_by_its_claimed_ordno_is_bound(tmp_config):
    import time as _time

    from hft_platform.order.adapter import _PhantomClaim, _PhantomEntry

    listed = _trade("0A11FF", order_datetime=dt.datetime(2026, 11, 12, 11, 34, 2))
    client = _ListingClient()
    client.listing = [listed]
    client.mode = "simulation"
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=8), broker_client=client)
    adapter.price_codec = _Codec()
    adapter._resolve_broker_contract_code = lambda intent: intent.symbol  # type: ignore[method-assign]  # test seam
    adapter.set_rejection_sink(asyncio.Queue(maxsize=16))
    adapter._spawn_phantom_bind = MagicMock()  # type: ignore[method-assign]  # record the decision only
    p1 = _command(6816).intent
    aged = _time.monotonic() - 999.0
    claim = _PhantomClaim(ordno="0A11FF", qty=1, fill_ts_ns=_FILL_TS_NS, claimed_at=aged)
    adapter._phantom_records[f"{_STRATEGY}:6816"] = [
        _PhantomEntry(monotonic_ts=aged, symbol="TMFL6", created_ns=0, intent=p1, side=int(Side.BUY), claim=claim)
    ]

    await adapter.release_stale_phantom_pendings(ttl_s=30.0)
    await adapter._phantom_lookup_task

    adapter._spawn_phantom_bind.assert_called_once()
    assert adapter._spawn_phantom_bind.call_args.args[1] is listed


# --------------------------------------------------------------------------- #
# Round 3: a claimed record whose ordno is not listed, and the claim ledger    #
# --------------------------------------------------------------------------- #


def _listing_adapter(tmp_config: str, listed: list[Any]) -> OrderAdapter:
    client = _ListingClient()
    client.listing = listed
    client.mode = "simulation"
    adapter = OrderAdapter(config_path=tmp_config, order_queue=asyncio.Queue(maxsize=8), broker_client=client)
    adapter.price_codec = _Codec()
    adapter._resolve_broker_contract_code = lambda intent: intent.symbol  # type: ignore[method-assign]  # test seam
    adapter._broker_codec = None
    adapter._add_to_dlq = AsyncMock()
    adapter._audit_writer = MagicMock()
    adapter.set_rejection_sink(asyncio.Queue(maxsize=16))
    return adapter


async def _lookup_and_binds(adapter: OrderAdapter, ttl_s: float = 0.0) -> None:
    await adapter.release_stale_phantom_pendings(ttl_s=ttl_s)
    task = adapter._phantom_lookup_task
    if task is not None:
        await task
    if adapter._phantom_bind_tasks:
        await asyncio.gather(*list(adapter._phantom_bind_tasks))


@pytest.mark.asyncio
async def test_claimed_phantom_whose_own_order_is_listed_by_params_is_cancelled_as_misattributed(tmp_config):
    """P BUY claimed a fill with ordno X; X is not listed; P's own order Y is listed open
    with P's parameters. Y is P's working order and the claim was wrong: cancel Y, page
    misattributed, and hand back no slot (the claimed fill's on_fill already did)."""
    own = _trade("0A1234")  # Y: P's params, another ordno
    adapter = _listing_adapter(tmp_config, [own])
    with adapter._phantom_lock:
        adapter._register_phantom(_command().intent)
    assert adapter.resolve_phantom_fill(_fill("0A11FF")) == _STRATEGY

    with patch("hft_platform.order.adapter.logger") as log:
        await _lookup_and_binds(adapter)

    adapter.client.cancel_order.assert_called_once()
    assert adapter.client.cancel_order.call_args.args[0] is own
    assert _claims(adapter) == ["tentative", "misattributed"]
    assert [c.args[0] for c in log.critical.call_args_list] == ["claim_misattributed"]
    assert adapter.order_finished(_KEY), "Y's own terminal releases nothing"
    assert [f for f in _feedback(adapter) if not f.was_approved] == [], "the fill's on_fill was the one release"
    assert _KEY not in adapter._live_order_intents


@pytest.mark.asyncio
async def test_claimed_phantom_with_two_param_matches_is_held_then_released_at_max_hold(tmp_config):
    adapter = _listing_adapter(tmp_config, [_trade("0A1234"), _trade("0A1235")])
    adapter._spawn_phantom_bind = MagicMock()  # type: ignore[method-assign]  # record the decision only
    with adapter._phantom_lock:
        adapter._register_phantom(_command().intent)
    adapter.resolve_phantom_fill(_fill("0A11FF"))
    adapter._phantom_lookup_max_hold_s = 3600.0

    await _lookup_and_binds(adapter)

    adapter._spawn_phantom_bind.assert_not_called()
    assert _KEY in adapter._phantom_records, "held as ambiguous"
    assert "lookup_ambiguous" in _bound(adapter)

    adapter._phantom_lookup_max_hold_s = 0.0
    with patch("hft_platform.order.adapter.logger") as log:
        await _lookup_and_binds(adapter)

    assert _KEY not in adapter._phantom_records
    assert _KEY in adapter._get_expired_claims(), "its claim waits for a late Trade"
    assert any(c.args[0] == "phantom_claim_ambiguous_released" for c in log.error.call_args_list)
    assert [f for f in _feedback(adapter) if not f.was_approved] == []


def test_prune_forgets_an_old_claim_stored_behind_a_fresh_one(tmp_config):
    """A claim moved from a live record keeps its original stamp, so the ledger is not
    in stamp order: an old claim can sit behind a fresh one."""
    import time as _time

    from hft_platform.order import adapter as adapter_module
    from hft_platform.order.adapter import _PhantomClaim

    adapter = _adapter(tmp_config, _LateBroker(None))
    now = _time.monotonic()
    old = now - adapter_module._EXPIRED_PHANTOM_RETENTION_S - 10.0
    adapter._stash_expired_claim("R47_MAKER_TMF:1", _PhantomClaim("0A0001", 1, 0, now))
    adapter._stash_expired_claim("R47_MAKER_TMF:2", _PhantomClaim("0A0002", 1, 0, old))

    adapter._prune_expired_claims()

    assert list(adapter._get_expired_claims()) == ["R47_MAKER_TMF:1"]
    assert _claims(adapter) == ["unverified"]


def test_restashing_a_key_counts_the_overwritten_claim_unverified(tmp_config):
    import time as _time

    from hft_platform.order.adapter import _PhantomClaim

    adapter = _adapter(tmp_config, _LateBroker(None))
    now = _time.monotonic()
    adapter._stash_expired_claim(_KEY, _PhantomClaim("0A0001", 1, 0, now))

    with patch("hft_platform.order.adapter.logger") as log:
        adapter._stash_expired_claim(_KEY, _PhantomClaim("0A0002", 1, 0, now))

    assert _claims(adapter) == ["unverified"]
    assert any(
        c.args[0] == "phantom_claim_unverified" and c.kwargs["claimed_ordno"] == "0A0001"
        for c in log.warning.call_args_list
    )
    assert adapter._get_expired_claims()[_KEY].ordno == "0A0002"


@pytest.mark.asyncio
async def test_trade_after_its_claim_was_evicted_cancels_without_a_second_release(tmp_config):
    """6816 claimed a fill (slot freed by on_fill), its record was dropped at TTL, then its
    claim was pushed out of the bounded ledger. Its Trade then arrives: the order is
    cancelled, and its terminal must not free R47's slot a second time."""
    import time as _time

    from hft_platform.order import adapter as adapter_module
    from hft_platform.order.adapter import _PhantomClaim

    broker = _LateBroker(_trade("0A1234"))
    adapter = _adapter(tmp_config, broker)
    assert await adapter._dispatch_to_api(_command()) is False
    _feedback(adapter)
    adapter.resolve_phantom_fill(_fill("0A11FF"))
    assert await adapter.release_stale_phantom_pendings(ttl_s=0.0) == 0
    assert _KEY in adapter._get_expired_claims()
    with patch.object(adapter_module, "_EXPIRED_PHANTOM_MAX", 1):
        adapter._stash_expired_claim(f"{_STRATEGY}:9999", _PhantomClaim("0B0000", 1, 0, _time.monotonic()))
    assert _KEY not in adapter._get_expired_claims()

    broker.release.set()
    await _until(lambda: _rows(adapter, "phantom_bound"))

    adapter.client.cancel_order.assert_called_once()
    assert adapter.order_finished(_KEY), "the terminal of the cancelled order releases nothing"
    assert _KEY not in adapter._live_order_intents
    assert [f for f in _feedback(adapter) if not f.was_approved] == []
    assert _claims(adapter).count("unverified") == 1
