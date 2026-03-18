"""Unit tests for broker_registry module."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from hft_platform.feed_adapter.broker_registry import (
    _BROKER_REGISTRY,
    BrokerFactory,
    get_broker_factory,
    list_brokers,
    register_broker,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubFactory:
    """Minimal BrokerFactory implementation for testing."""

    __slots__ = ("_tag",)

    def __init__(self, tag: str = "stub") -> None:
        self._tag = tag

    def create_clients(self, symbols_path: str, broker_config: dict[str, Any]) -> tuple[Any, Any]:
        return (self._tag, self._tag)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure global registry is reset between tests."""
    saved = dict(_BROKER_REGISTRY)
    _BROKER_REGISTRY.clear()
    yield
    _BROKER_REGISTRY.clear()
    _BROKER_REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegisterBroker:
    def test_register_and_retrieve(self):
        factory = _StubFactory("alpha")
        register_broker("Alpha", factory)
        result = get_broker_factory("alpha")
        assert result is factory

    def test_register_case_insensitive(self):
        factory = _StubFactory()
        register_broker("SHIOAJI", factory)
        assert get_broker_factory("shioaji") is factory
        assert get_broker_factory("Shioaji") is factory

    def test_register_overwrites(self):
        f1 = _StubFactory("v1")
        f2 = _StubFactory("v2")
        register_broker("test", f1)
        register_broker("test", f2)
        assert get_broker_factory("test") is f2


class TestGetBrokerFactory:
    def test_unknown_broker_raises(self):
        with pytest.raises(ValueError, match="Unknown broker"):
            get_broker_factory("nonexistent")

    def test_env_var_fallback(self):
        factory = _StubFactory("env")
        register_broker("env_broker", factory)
        with patch.dict("os.environ", {"HFT_BROKER": "env_broker"}):
            assert get_broker_factory() is factory

    def test_default_broker_when_no_env(self):
        factory = _StubFactory("shioaji")
        register_broker("shioaji", factory)
        with patch.dict("os.environ", {}, clear=True):
            assert get_broker_factory() is factory

    def test_explicit_name_overrides_env(self):
        f_env = _StubFactory("env")
        f_explicit = _StubFactory("explicit")
        register_broker("env_broker", f_env)
        register_broker("explicit_broker", f_explicit)
        with patch.dict("os.environ", {"HFT_BROKER": "env_broker"}):
            assert get_broker_factory("explicit_broker") is f_explicit


class TestListBrokers:
    def test_empty_registry(self):
        assert list_brokers() == []

    def test_sorted_names(self):
        register_broker("zulu", _StubFactory())
        register_broker("alpha", _StubFactory())
        register_broker("mike", _StubFactory())
        assert list_brokers() == ["alpha", "mike", "zulu"]


class TestBrokerFactoryProtocol:
    def test_stub_satisfies_protocol(self):
        factory = _StubFactory()
        assert isinstance(factory, BrokerFactory)

    def test_create_clients_returns_tuple(self):
        factory = _StubFactory("test")
        md, oe = factory.create_clients("symbols.yaml", {})
        assert md == "test"
        assert oe == "test"
