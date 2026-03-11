"""Tests for the broker registry and factory protocol."""

from __future__ import annotations

from typing import Any

import pytest

from hft_platform.feed_adapter.broker_registry import (
    _BROKER_REGISTRY,
    BrokerFactory,
    get_broker_factory,
    list_brokers,
    register_broker,
)


class _StubFactory:
    """Minimal BrokerFactory-compliant stub for testing."""

    def create_clients(
        self,
        symbols_path: str,
        broker_config: dict[str, Any],
    ) -> tuple[Any, Any]:
        return ("md_client", "order_client")


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure each test starts with a clean broker registry."""
    saved = dict(_BROKER_REGISTRY)
    _BROKER_REGISTRY.clear()
    yield
    _BROKER_REGISTRY.clear()
    _BROKER_REGISTRY.update(saved)


class TestRegisterBroker:
    def test_register_and_retrieve(self) -> None:
        factory = _StubFactory()
        register_broker("demo", factory)
        assert get_broker_factory("demo") is factory

    def test_register_case_insensitive(self) -> None:
        factory = _StubFactory()
        register_broker("Demo", factory)
        assert get_broker_factory("demo") is factory
        assert get_broker_factory("DEMO") is factory

    def test_overwrite_does_not_crash(self) -> None:
        f1 = _StubFactory()
        f2 = _StubFactory()
        register_broker("broker", f1)
        register_broker("broker", f2)
        assert get_broker_factory("broker") is f2


class TestGetBrokerFactory:
    def test_unknown_broker_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown broker 'nope'"):
            get_broker_factory("nope")

    def test_error_lists_registered_brokers(self) -> None:
        register_broker("alpha", _StubFactory())
        register_broker("beta", _StubFactory())
        with pytest.raises(ValueError, match=r"\['alpha', 'beta'\]"):
            get_broker_factory("missing")

    def test_respects_hft_broker_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = _StubFactory()
        register_broker("envbroker", factory)
        monkeypatch.setenv("HFT_BROKER", "envbroker")
        assert get_broker_factory() is factory

    def test_env_var_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = _StubFactory()
        register_broker("envbroker", factory)
        monkeypatch.setenv("HFT_BROKER", "EnvBroker")
        assert get_broker_factory() is factory

    def test_defaults_to_shioaji(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_BROKER", raising=False)
        factory = _StubFactory()
        register_broker("shioaji", factory)
        assert get_broker_factory() is factory

    def test_explicit_name_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f_env = _StubFactory()
        f_explicit = _StubFactory()
        register_broker("fromenv", f_env)
        register_broker("explicit", f_explicit)
        monkeypatch.setenv("HFT_BROKER", "fromenv")
        assert get_broker_factory("explicit") is f_explicit


class TestListBrokers:
    def test_empty_registry(self) -> None:
        assert list_brokers() == []

    def test_sorted_output(self) -> None:
        register_broker("zebra", _StubFactory())
        register_broker("alpha", _StubFactory())
        register_broker("middle", _StubFactory())
        assert list_brokers() == ["alpha", "middle", "zebra"]


class TestBrokerFactoryProtocol:
    def test_isinstance_check(self) -> None:
        assert isinstance(_StubFactory(), BrokerFactory)

    def test_non_conforming_rejects(self) -> None:
        class _Bad:
            pass

        assert not isinstance(_Bad(), BrokerFactory)

    def test_create_clients_returns_tuple(self) -> None:
        factory = _StubFactory()
        md, order = factory.create_clients("symbols.yaml", {})
        assert md == "md_client"
        assert order == "order_client"
