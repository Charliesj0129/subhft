"""The broker HFT_BROKER selects must also decide how its execution reports are read.

``ExecutionRouter`` built its ``ExecutionNormalizer`` without a field map, and bootstrap handed it
none, so the normalizer always read reports with the Shioaji key names.  Under ``HFT_BROKER=fubon``
the Fubon callback adapter delivers the trading symbol under ``symbol``, a key the Shioaji map does
not list, so every order and deal report normalized to ``symbol='UNKNOWN'``.

``get_field_map`` falls back to the Shioaji map for a name it does not know, so the fail-closed
guard for a bad broker name is ``SystemBootstrapper._resolve_broker_id``, which raises before the
router is built.  These tests run the whole path: HFT_BROKER -> SystemBootstrapper.build ->
ExecutionRouter field_map -> normalizer, with the report shape the broker adapter really emits.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.fubon_mock_helper import BSAction, install_fubon_neo_mock, make_fubon_sdk_mock

install_fubon_neo_mock()

from hft_platform.contracts.execution import Side
from hft_platform.execution.field_map import FubonExecFieldMap, ShioajiExecFieldMap
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.router import ExecutionRouter
from hft_platform.feed_adapter.fubon.execution_callbacks import (
    FubonExecutionCallbackAdapter,
    bridge_order_callback,
)
from hft_platform.services.bootstrap import SystemBootstrapper

_SERVICE_TARGETS = (
    "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
    "hft_platform.feed_adapter.shioaji.quote_connection_pool.QuoteConnectionPool",
    "hft_platform.services.bootstrap.MarketDataService",
    "hft_platform.services.bootstrap.OrderAdapter",
    "hft_platform.services.bootstrap.ExecutionGateway",
    "hft_platform.services.bootstrap.ExecutionRouter",
    "hft_platform.services.bootstrap.RiskEngine",
    "hft_platform.services.bootstrap.ReconciliationService",
    "hft_platform.services.bootstrap.StrategyRunner",
    "hft_platform.services.bootstrap.RecorderService",
    "hft_platform.services.bootstrap.RingBufferBus",
    "hft_platform.services.bootstrap.PositionStore",
    "hft_platform.services.bootstrap.StormGuard",
    "hft_platform.services.bootstrap.SymbolMetadata",
    "hft_platform.services.bootstrap.SymbolMetadataPriceScaleProvider",
    "hft_platform.services.bootstrap.LatencyRecorder",
    "hft_platform.services.bootstrap.FeatureEngine",
)
_INGEST_NS = 1_700_000_000_000_000_000
_SYMBOLS_YAML = "symbols:\n  - code: 'TXFL6'\n    exchange: 'TAIFEX'\n    price_scale: 10000\n"


@pytest.fixture()
def service_mocks() -> Iterator[dict[str, MagicMock]]:
    """Patch the heavy service constructors so build() never touches real infrastructure."""
    with ExitStack() as stack:
        yield {target.rsplit(".", 1)[1]: stack.enter_context(patch(target)) for target in _SERVICE_TARGETS}


@pytest.fixture()
def symbols_config(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(_SYMBOLS_YAML)
    monkeypatch.setenv("SYMBOLS_CONFIG", str(cfg))
    return str(cfg)


_FUBON_LAZY_MODULES = (
    "hft_platform.feed_adapter.fubon",
    "hft_platform.feed_adapter.fubon.facade",
    "hft_platform.feed_adapter.fubon.order_codec",
)


def _build(monkeypatch: pytest.MonkeyPatch, broker: str | None) -> None:
    monkeypatch.setenv("HFT_MODE", "sim")
    monkeypatch.setenv("HFT_ORDER_MODE", "sim")
    monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
    monkeypatch.delenv("HFT_GATEWAY_ENABLED", raising=False)
    if broker is None:
        monkeypatch.delenv("HFT_BROKER", raising=False)
    else:
        monkeypatch.setenv("HFT_BROKER", broker)
    if broker == "fubon":
        # Stand-ins for the lazily imported Fubon facade and codec. Only these keys are swapped:
        # patch.dict("sys.modules") would also forget every module build() imports, and a second
        # build() in the same process would then register its Prometheus metrics twice.
        for name in _FUBON_LAZY_MODULES:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    bootstrapper = SystemBootstrapper({})
    with patch.object(bootstrapper, "_check_session_ownership", return_value=False):
        bootstrapper.build()


def _router_kwargs_from_build(
    monkeypatch: pytest.MonkeyPatch, service_mocks: dict[str, MagicMock], broker: str | None
) -> dict[str, Any]:
    _build(monkeypatch, broker)
    router_cls = service_mocks["ExecutionRouter"]
    router_cls.assert_called_once()
    return dict(router_cls.call_args.kwargs)


def _real_router(**extra: Any) -> ExecutionRouter:
    return ExecutionRouter(
        bus=None,
        raw_queue=asyncio.Queue(),
        order_id_map={},
        position_store=None,
        terminal_handler=lambda *_: None,
        default_account_id_provider=lambda: "ACCT-1",
        **extra,
    )


def _router_wired_by_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: dict[str, MagicMock],
    symbols_config: str,
    broker: str | None,
) -> ExecutionRouter:
    """A real router given the field map bootstrap handed the (mocked) router class, if it handed one."""
    kwargs = _router_kwargs_from_build(monkeypatch, service_mocks, broker)
    # build() repoints SYMBOLS_CONFIG at the operator's file; the real router reads the test's.
    monkeypatch.setenv("SYMBOLS_CONFIG", symbols_config)
    return _real_router(**({"field_map": kwargs["field_map"]} if "field_map" in kwargs else {}))


def _fubon_reports() -> tuple[dict[str, Any], dict[str, Any]]:
    """An order envelope and a deal payload exactly as FubonExecutionCallbackAdapter delivers them."""
    sdk = make_fubon_sdk_mock()
    orders: list[dict[str, Any]] = []
    deals: list[dict[str, Any]] = []
    adapter = FubonExecutionCallbackAdapter(sdk)
    adapter.register(
        on_order=bridge_order_callback(lambda state, payload: orders.append({"state": state, "payload": payload})),
        on_deal=deals.append,
    )
    sdk.set_on_futopt_order.call_args.args[0](
        "0",
        SimpleNamespace(
            order_no="l0001",
            seq_no="00000000001",
            symbol="FITX",
            expiry_date="202612",
            strike_price=None,
            call_put=None,
            symbol_leg2=None,
            buy_sell=BSAction.Buy,
            price=22000.0,
            lot=2,
            status=10,
            function_type=0,
            user_def="a1b2c3",
        ),
    )
    sdk.set_on_futopt_filled.call_args.args[0](
        None,
        SimpleNamespace(
            order_no="l0001",
            seq_no="00000000001",
            symbol="FITX",
            expiry_date="202612",
            strike_price=None,
            call_put=None,
            symbol_leg2=None,
            buy_sell=BSAction.Sell,
            filled_no="00000000007",
            filled_lots=2,
            filled_price=22000.0,
            filled_time="10:31:00.931",
            user_def="a1b2c3",
        ),
    )
    return orders[0], deals[0]


def _shioaji_order_report() -> dict[str, Any]:
    """The Shioaji order-callback shape: the symbol sits under contract.code."""
    return {
        "order": {"ordno": "SJ0001", "action": "Buy", "price": 22000.0, "quantity": 2},
        "contract": {"code": "TXFL6"},
        "status": {"status": "Submitted"},
    }


def test_bootstrap_passes_the_fubon_field_map_to_the_router_when_broker_is_fubon(
    monkeypatch: pytest.MonkeyPatch, service_mocks: dict[str, MagicMock]
) -> None:
    kwargs = _router_kwargs_from_build(monkeypatch, service_mocks, "fubon")

    assert isinstance(kwargs.get("field_map"), FubonExecFieldMap)


def test_router_wired_by_bootstrap_normalizes_a_fubon_order_report_to_the_trading_symbol(
    monkeypatch: pytest.MonkeyPatch, service_mocks: dict[str, MagicMock], symbols_config: str
) -> None:
    order_envelope, _ = _fubon_reports()
    router = _router_wired_by_bootstrap(monkeypatch, service_mocks, symbols_config, "fubon")

    event = router.normalizer.normalize_order(RawExecEvent("order", order_envelope, _INGEST_NS))

    assert event is not None
    assert (event.symbol, event.side, event.submitted_qty) == ("TXFL6", Side.BUY, 2)


def test_router_wired_by_bootstrap_normalizes_a_fubon_deal_report_to_the_trading_symbol(
    monkeypatch: pytest.MonkeyPatch, service_mocks: dict[str, MagicMock], symbols_config: str
) -> None:
    _, deal_payload = _fubon_reports()
    router = _router_wired_by_bootstrap(monkeypatch, service_mocks, symbols_config, "fubon")

    fill = router.normalizer.normalize_fill(RawExecEvent("deal", deal_payload, time.time_ns()))

    assert fill is not None
    assert (fill.symbol, fill.side, fill.qty) == ("TXFL6", Side.SELL, 2)


@pytest.mark.parametrize("broker", ["shioaji", None], ids=["broker-shioaji", "broker-unset"])
def test_bootstrap_passes_the_shioaji_field_map_to_the_router_when_broker_is_shioaji_or_unset(
    monkeypatch: pytest.MonkeyPatch, service_mocks: dict[str, MagicMock], broker: str | None
) -> None:
    kwargs = _router_kwargs_from_build(monkeypatch, service_mocks, broker)

    field_map = kwargs.get("field_map")
    assert isinstance(field_map, ShioajiExecFieldMap)
    assert not isinstance(field_map, FubonExecFieldMap)
    assert field_map.symbol_keys() == ("full_code", "code")


@pytest.mark.parametrize("broker", ["shioaji", None], ids=["broker-shioaji", "broker-unset"])
def test_router_wired_by_bootstrap_still_reads_a_shioaji_order_report_by_contract_code(
    monkeypatch: pytest.MonkeyPatch, service_mocks: dict[str, MagicMock], symbols_config: str, broker: str | None
) -> None:
    router = _router_wired_by_bootstrap(monkeypatch, service_mocks, symbols_config, broker)

    event = router.normalizer.normalize_order(RawExecEvent("order", _shioaji_order_report(), _INGEST_NS))

    assert event is not None
    assert (event.symbol, event.order_id, event.side) == ("TXFL6", "SJ0001", Side.BUY)


def test_router_wired_by_bootstrap_does_not_read_the_fubon_symbol_key_when_broker_is_shioaji(
    monkeypatch: pytest.MonkeyPatch, service_mocks: dict[str, MagicMock], symbols_config: str
) -> None:
    order_envelope, _ = _fubon_reports()
    router = _router_wired_by_bootstrap(monkeypatch, service_mocks, symbols_config, "shioaji")

    event = router.normalizer.normalize_order(RawExecEvent("order", order_envelope, _INGEST_NS))

    assert event is not None
    assert event.symbol == "UNKNOWN"


def test_bootstrap_builds_no_router_when_broker_is_unknown(
    monkeypatch: pytest.MonkeyPatch, service_mocks: dict[str, MagicMock]
) -> None:
    with pytest.raises(ValueError, match="Unknown HFT_BROKER"):
        _build(monkeypatch, "not_a_broker")

    service_mocks["ExecutionRouter"].assert_not_called()


def test_router_hands_the_given_field_map_to_its_normalizer(symbols_config: str) -> None:
    field_map = FubonExecFieldMap()

    router = _real_router(field_map=field_map)

    assert router.normalizer.field_map is field_map


def test_router_without_a_field_map_keeps_the_shioaji_default(symbols_config: str) -> None:
    router = _real_router()

    assert isinstance(router.normalizer.field_map, ShioajiExecFieldMap)
