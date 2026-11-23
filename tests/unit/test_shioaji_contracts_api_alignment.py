from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog.testing
import yaml

from hft_platform.feed_adapter.shioaji import contracts_runtime
from hft_platform.feed_adapter.shioaji._infra import reset_login_slot_for_tests
from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime, derive_callback_code


class _NotAuthenticated(Exception):
    pass


class _Contracts:
    def __init__(self, *, loaded: bool, status: Any = None, expose_status: bool = True) -> None:
        groups = [SimpleNamespace(code="X")] if loaded else []
        self.Futures = list(groups)
        self.Options = list(groups)
        self.Stocks = list(groups)
        if expose_status:
            self.status = status


class _CountingContracts:
    def __init__(self, status: Any) -> None:
        self.status = status
        self.category_reads = 0

    @property
    def Futures(self) -> Any:
        self.category_reads += 1
        return [SimpleNamespace(code="X")]

    Options = Futures
    Stocks = Futures


class _LoggedInApi:
    def __init__(self, contracts: _Contracts) -> None:
        self.Contracts = contracts
        self.fetch_contracts = MagicMock()
        self.login = MagicMock()


class _PreLoginApi:
    def __init__(self) -> None:
        self.fetch_contracts = MagicMock()
        self.login = MagicMock()

    @property
    def Contracts(self) -> Any:
        raise _NotAuthenticated("Not authenticated")


@pytest.fixture(autouse=True)
def _clean_login_slot():
    reset_login_slot_for_tests()
    yield
    reset_login_slot_for_tests()


@pytest.fixture(autouse=True)
def _clean_target_code_warning_state(monkeypatch):
    monkeypatch.setattr(contracts_runtime, "_TARGET_CODE_MISMATCH_WARNED", set(), raising=False)


@pytest.fixture()
def make_client(tmp_path: Path):
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(yaml.dump({"symbols": [{"code": "2330", "exchange": "TSE"}]}))
    made: list[Any] = []

    def _make() -> Any:
        c = ShioajiClient(config_path=str(cfg))
        c.metrics = MagicMock()
        made.append(c)
        return c

    yield _make
    for c in made:
        c.close()


@pytest.fixture()
def client(make_client):
    return make_client()


def _runtime(api: Any, metrics: Any = None) -> ContractsRuntime:
    stub: Any = SimpleNamespace(api=api, metrics=metrics, _contract_update_last_event_s=0.0)
    return ContractsRuntime(stub)


def test_unsupported_contract_event_hook_warns_once_per_runtime():
    runtime = _runtime(SimpleNamespace())

    with structlog.testing.capture_logs() as logs:
        first = runtime.register_contract_event_callback()
        second = runtime.register_contract_event_callback()

    unsupported = [e for e in logs if e.get("event") == "contract_event_callback_unsupported"]
    assert first is False
    assert second is False
    assert len(unsupported) == 1
    assert unsupported[0]["log_level"] == "warning"
    assert unsupported[0]["reason"] == "sdk_exposes_no_contract_update_hook"


def test_unsupported_contract_event_hook_publishes_the_unregistered_gauge():
    metrics = SimpleNamespace(contract_event_callback_registered=MagicMock())
    runtime = _runtime(SimpleNamespace(), metrics)

    assert runtime.register_contract_event_callback() is False

    metrics.contract_event_callback_registered.labels.assert_called_once_with(conn_id="-")
    metrics.contract_event_callback_registered.labels.return_value.set.assert_called_once_with(0)


def test_unsupported_contract_event_warning_names_the_blind_facade():
    stub: Any = SimpleNamespace(api=SimpleNamespace(), metrics=None, _contract_update_last_event_s=0.0, conn_id=3)
    runtime = ContractsRuntime(stub)

    with structlog.testing.capture_logs() as logs:
        runtime.register_contract_event_callback()

    unsupported = [e for e in logs if e.get("event") == "contract_event_callback_unsupported"]
    assert [e["conn_id"] for e in unsupported] == ["3"]


def test_installed_sdk_class_exposes_no_contract_update_hook():
    sj = pytest.importorskip("shioaji")

    assert not hasattr(sj.Shioaji, "set_contract_event_callback")
    assert not hasattr(sj.Shioaji, "on_contract_event")
    assert hasattr(sj.Shioaji, "set_event_callback")


@pytest.mark.parametrize(
    "status", ["Unfetch", "Fetching", "Fetched", None], ids=["unfetch", "fetching", "fetched", "none"]
)
def test_contracts_ready_keeps_the_attribute_semantics_whatever_the_fetch_status(client, status):
    client.api = _LoggedInApi(_Contracts(loaded=False, status=status))

    assert client.contracts_ready is True


def test_contracts_ready_false_when_the_contracts_attribute_is_missing(client):
    client.api = SimpleNamespace()

    assert client.contracts_ready is False


def test_contracts_ready_false_when_api_is_none(client):
    client.api = None

    assert client.contracts_ready is False


def test_contracts_ready_false_when_the_sdk_raises_before_login(client):
    client.api = _PreLoginApi()

    assert client.contracts_ready is False


def test_contracts_ready_reads_neither_status_nor_categories(client):
    contracts = _CountingContracts("Unfetch")
    client.api = _LoggedInApi(contracts)

    assert client.contracts_ready is True
    assert contracts.category_reads == 0


def test_contracts_ready_warns_once_per_client_when_the_contracts_read_raises(client):
    client.api = _PreLoginApi()

    with structlog.testing.capture_logs() as logs:
        first = client.contracts_ready
        second = client.contracts_ready

    probes = [e for e in logs if e.get("event") == "contracts_probe_failed"]
    assert first is False
    assert second is False
    assert len(probes) == 1
    assert probes[0]["log_level"] == "warning"
    assert "Not authenticated" in probes[0]["error"]


def test_place_order_gate_follows_contracts_ready_not_the_loaded_state(client):
    from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway

    client.api = _LoggedInApi(_Contracts(loaded=False, status="Unfetch"))
    gateway = OrderGateway(client)

    assert client.contracts_loaded_state is False
    with pytest.raises(ValueError, match="not found"):
        gateway.place_order(
            contract_code="TMFR1",
            exchange="FUT",
            action="Buy",
            price=200000,
            qty=1,
            order_type="LMT",
            tif="ROD",
            product_type="future",
        )


def test_contracts_loaded_state_false_when_status_unfetched_and_nothing_loaded(client):
    client.api = _LoggedInApi(_Contracts(loaded=False, status="Unfetch"))

    assert client.contracts_loaded_state is False


def test_contracts_loaded_state_false_when_status_fetching_and_nothing_loaded(client):
    client.api = _LoggedInApi(_Contracts(loaded=False, status="Fetching"))

    assert client.contracts_loaded_state is False


def test_contracts_loaded_state_true_when_status_is_fetched(client):
    client.api = _LoggedInApi(_Contracts(loaded=False, status="Fetched"))

    assert client.contracts_loaded_state is True


@pytest.mark.parametrize(("member", "expected"), [("Fetched", True), ("Fetching", False), ("Unfetch", False)])
def test_contracts_loaded_state_reads_the_real_fetchstatus_enum(client, member, expected):
    sj = pytest.importorskip("shioaji")
    client.api = _LoggedInApi(_Contracts(loaded=False, status=getattr(sj.FetchStatus, member)))

    assert client.contracts_loaded_state is expected


def test_contracts_loaded_state_true_when_loaded_contracts_expose_no_status(client):
    client.api = _LoggedInApi(_Contracts(loaded=True, expose_status=False))

    assert client.contracts_loaded_state is True


def test_contracts_loaded_state_false_when_no_status_and_nothing_loaded(client):
    client.api = _LoggedInApi(_Contracts(loaded=False, expose_status=False))

    assert client.contracts_loaded_state is False


def test_contracts_loaded_state_follows_a_pending_status_even_when_categories_resolve(client):
    client.api = _LoggedInApi(_Contracts(loaded=True, status="Fetching"))

    assert client.contracts_loaded_state is False


@pytest.mark.parametrize(("status", "expected"), [("Fetched", True), ("Fetching", False), ("Unfetch", False)])
def test_contracts_loaded_state_does_not_read_categories_when_status_is_recognized(client, status, expected):
    contracts = _CountingContracts(status)
    client.api = _LoggedInApi(contracts)

    assert client.contracts_loaded_state is expected
    assert contracts.category_reads == 0


_UNRECOGNIZED_STATUSES = [None, "", "Bogus", 3, MagicMock()]
_UNRECOGNIZED_IDS = ["none", "empty", "unknown-string", "int", "mock"]


@pytest.mark.parametrize("status", _UNRECOGNIZED_STATUSES, ids=_UNRECOGNIZED_IDS)
def test_contracts_loaded_state_falls_back_to_categories_when_status_is_unrecognized_and_loaded(client, status):
    client.api = _LoggedInApi(_Contracts(loaded=True, status=status))

    assert client.contracts_loaded_state is True


@pytest.mark.parametrize("status", _UNRECOGNIZED_STATUSES, ids=_UNRECOGNIZED_IDS)
def test_contracts_loaded_state_falls_back_to_categories_when_status_is_unrecognized_and_empty(client, status):
    client.api = _LoggedInApi(_Contracts(loaded=False, status=status))

    assert client.contracts_loaded_state is False


def test_contracts_loaded_state_false_when_the_sdk_raises_before_login(client):
    client.api = _PreLoginApi()

    assert client.contracts_loaded_state is False


def test_contracts_loaded_state_false_when_api_is_none(client):
    client.api = None

    assert client.contracts_loaded_state is False


def test_contracts_loaded_state_does_no_broker_io(client):
    api = _LoggedInApi(_Contracts(loaded=False, status="Unfetch"))
    client.api = api

    assert client.contracts_loaded_state is False

    api.fetch_contracts.assert_not_called()
    api.login.assert_not_called()


def test_contracts_loaded_state_warns_once_per_client_when_the_contracts_read_raises(client):
    client.api = _PreLoginApi()

    with structlog.testing.capture_logs() as logs:
        first = client.contracts_loaded_state
        second = client.contracts_loaded_state

    probes = [e for e in logs if e.get("event") == "contracts_probe_failed"]
    assert first is False
    assert second is False
    assert len(probes) == 1
    assert probes[0]["log_level"] == "warning"
    assert "Not authenticated" in probes[0]["error"]


def test_contracts_probe_warning_is_shared_by_both_properties_of_one_client(client):
    client.api = _PreLoginApi()

    with structlog.testing.capture_logs() as logs:
        client.contracts_ready
        client.contracts_loaded_state

    assert [e["event"] for e in logs if e.get("event") == "contracts_probe_failed"] == ["contracts_probe_failed"]


def test_contracts_probe_warning_is_per_client(make_client):
    first = make_client()
    second = make_client()
    first.api = _PreLoginApi()
    second.api = _PreLoginApi()

    with structlog.testing.capture_logs() as logs:
        first.contracts_loaded_state
        second.contracts_loaded_state

    assert len([e for e in logs if e.get("event") == "contracts_probe_failed"]) == 2


def _mismatch_events(logs: list[Any]) -> list[Any]:
    return [e for e in logs if e.get("event") == "contract_target_code_mismatch"]


def test_alias_keeps_the_delivery_month_code_when_target_code_disagrees():
    contract = SimpleNamespace(code="TMFR1", delivery_month="2026/05", target_code="TMFF6")

    with structlog.testing.capture_logs() as logs:
        assert derive_callback_code(contract, "TMFR1") == "TMFE6"

    events = _mismatch_events(logs)
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["alias"] == "TMFR1"
    assert events[0]["target_code"] == "TMFF6"
    assert events[0]["delivery_code"] == "TMFE6"


def test_alias_keeps_the_delivery_date_code_when_target_code_disagrees():
    contract = SimpleNamespace(code="TMFR1", delivery_date="2026/06/17", target_code="TMFE6")

    with structlog.testing.capture_logs() as logs:
        assert derive_callback_code(contract, "TMFR1") == "TMFF6"

    events = _mismatch_events(logs)
    assert [(e["target_code"], e["delivery_code"]) for e in events] == [("TMFE6", "TMFF6")]


def test_alias_agreeing_target_code_returns_the_same_code_without_a_warning():
    contract = SimpleNamespace(code="TMFR1", delivery_month="2026/05", target_code="TMFE6")

    with structlog.testing.capture_logs() as logs:
        assert derive_callback_code(contract, "TMFR1") == "TMFE6"

    assert _mismatch_events(logs) == []


def test_alias_target_code_whitespace_is_ignored_when_comparing():
    contract = SimpleNamespace(code="TMFR1", delivery_month="2026/05", target_code=" TMFE6 ")

    with structlog.testing.capture_logs() as logs:
        assert derive_callback_code(contract, "TMFR1") == "TMFE6"

    assert _mismatch_events(logs) == []


def test_alias_target_code_mismatch_warns_once_per_alias():
    contract = SimpleNamespace(code="TMFR1", delivery_month="2026/05", target_code="TMFF6")
    other = SimpleNamespace(code="TXFR1", delivery_month="2026/05", target_code="TXFF6")

    with structlog.testing.capture_logs() as logs:
        derive_callback_code(contract, "TMFR1")
        derive_callback_code(contract, "TMFR1")
        derive_callback_code(other, "TXFR1")

    assert [e["alias"] for e in _mismatch_events(logs)] == ["TMFR1", "TXFR1"]


@pytest.mark.parametrize("target", [None, "", "   ", 7, MagicMock()], ids=["none", "empty", "blank", "int", "mock"])
def test_alias_without_a_usable_target_code_returns_the_delivery_code_silently(target):
    contract = SimpleNamespace(code="TMFR1", delivery_month="2026/05", target_code=target)

    with structlog.testing.capture_logs() as logs:
        assert derive_callback_code(contract, "TMFR1") == "TMFE6"

    assert _mismatch_events(logs) == []


def test_alias_without_delivery_info_ignores_target_code_and_uses_the_contract_code():
    contract = SimpleNamespace(code="TMFR1", target_code="TMFF6")

    with structlog.testing.capture_logs() as logs:
        assert derive_callback_code(contract, "TMFR1") == "TMFR1"

    assert _mismatch_events(logs) == []


def test_target_code_mismatch_tracking_is_capped(monkeypatch):
    monkeypatch.setattr(contracts_runtime, "_TARGET_CODE_MISMATCH_WARN_CAP", 1)
    first = SimpleNamespace(code="TMFR1", delivery_month="2026/05", target_code="TMFF6")
    second = SimpleNamespace(code="TXFR1", delivery_month="2026/05", target_code="TXFF6")

    with structlog.testing.capture_logs() as logs:
        derive_callback_code(first, "TMFR1")
        assert derive_callback_code(second, "TXFR1") == "TXFE6"

    assert [e["alias"] for e in _mismatch_events(logs)] == ["TMFR1"]


def test_regular_month_code_ignores_target_code():
    contract = SimpleNamespace(code="TMFE6", delivery_month="2026/05", target_code="TMFF6")

    with structlog.testing.capture_logs() as logs:
        assert derive_callback_code(contract, "TMFE6") == "TMFE6"

    assert _mismatch_events(logs) == []
