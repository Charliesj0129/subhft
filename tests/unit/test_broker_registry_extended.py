"""Extended tests for broker_registry module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.broker_registry import (
    _BROKER_REGISTRY,
    BrokerFactory,
    get_broker_factory,
    list_brokers,
    register_broker,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Snapshot and restore global registry between tests."""
    original = dict(_BROKER_REGISTRY)
    yield
    _BROKER_REGISTRY.clear()
    _BROKER_REGISTRY.update(original)


def _make_factory() -> BrokerFactory:
    """Create a mock that satisfies BrokerFactory protocol."""
    factory = MagicMock(spec=BrokerFactory)
    factory.create_clients = MagicMock(return_value=(MagicMock(), MagicMock()))
    return factory


class TestRegisterBroker:
    def test_stores_factory_case_insensitive(self) -> None:
        factory = _make_factory()
        register_broker("MyBroker", factory)
        assert "mybroker" in _BROKER_REGISTRY
        assert _BROKER_REGISTRY["mybroker"] is factory

    def test_overwrites_existing_entry(self) -> None:
        factory_a = _make_factory()
        factory_b = _make_factory()
        register_broker("demo", factory_a)
        register_broker("demo", factory_b)
        assert _BROKER_REGISTRY["demo"] is factory_b

    def test_mixed_case_normalized_to_lowercase(self) -> None:
        factory = _make_factory()
        register_broker("FuBon", factory)
        assert "fubon" in _BROKER_REGISTRY
        assert "FuBon" not in _BROKER_REGISTRY


class TestGetBrokerFactory:
    def test_retrieves_registered_factory(self) -> None:
        factory = _make_factory()
        register_broker("test_broker", factory)
        result = get_broker_factory("test_broker")
        assert result is factory

    def test_unknown_name_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown broker"):
            get_broker_factory("nonexistent")

    def test_error_message_includes_registered_brokers(self) -> None:
        register_broker("alpha", _make_factory())
        register_broker("beta", _make_factory())
        with pytest.raises(ValueError, match=r"Registered: \['alpha', 'beta'\]"):
            get_broker_factory("missing")

    def test_none_uses_hft_broker_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = _make_factory()
        register_broker("envbroker", factory)
        monkeypatch.setenv("HFT_BROKER", "envbroker")
        result = get_broker_factory(None)
        assert result is factory

    def test_defaults_to_shioaji_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_BROKER", raising=False)
        factory = _make_factory()
        register_broker("shioaji", factory)
        result = get_broker_factory(None)
        assert result is factory


class TestListBrokers:
    def test_returns_sorted_names(self) -> None:
        register_broker("zebra", _make_factory())
        register_broker("alpha", _make_factory())
        register_broker("middle", _make_factory())
        result = list_brokers()
        assert result == ["alpha", "middle", "zebra"]

    def test_empty_after_clear(self) -> None:
        _BROKER_REGISTRY.clear()
        assert list_brokers() == []
