"""Integration tests for multi-broker switching mechanism.

Verifies that the broker registry correctly handles registration,
lookup, environment-variable-based selection, and protocol conformance
without requiring any real broker SDK.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.broker_registry import (
    BrokerFactory,
    _BROKER_REGISTRY,
    get_broker_factory,
    list_brokers,
    register_broker,
)

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _MockMarketDataProvider:
    """Minimal mock satisfying MarketDataProvider protocol shape."""

    __slots__ = ()

    def subscribe_basket(self, cb: Any) -> None:
        pass

    def fetch_snapshots(self) -> list:
        return []

    def resubscribe(self) -> bool:
        return True

    def reload_symbols(self) -> None:
        pass

    def validate_symbols(self) -> list:
        return []


class _MockOrderExecutor:
    """Minimal mock satisfying OrderExecutor protocol shape."""

    __slots__ = ()

    def place_order(
        self,
        contract_code: str,
        exchange: str,
        action: str,
        price: int,
        qty: int,
        order_type: str = "stock",
        tif: str = "ROD",
        **kwargs: Any,
    ) -> None:
        return None

    def cancel_order(self, trade: Any) -> None:
        return None

    def update_order(
        self,
        trade: Any,
        price: int | None = None,
        qty: int | None = None,
    ) -> None:
        return None

    def get_exchange(self, symbol: str) -> str:
        return "TSE"

    def set_execution_callbacks(self, on_order: Any, on_deal: Any) -> None:
        pass


class _MockAccountProvider:
    """Minimal mock satisfying AccountProvider protocol shape."""

    __slots__ = ()

    def get_positions(self) -> list:
        return []

    def get_account_balance(self) -> int:
        return 0

    def get_margin(self) -> int:
        return 0

    def list_position_detail(self) -> list:
        return []

    def list_profit_loss(self) -> list:
        return []


class _MockBrokerSession:
    """Minimal mock satisfying BrokerSession protocol shape."""

    __slots__ = ("_logged_in",)

    def __init__(self) -> None:
        self._logged_in = False

    def login(self) -> None:
        self._logged_in = True

    def reconnect(self) -> None:
        pass

    def close(self) -> None:
        self._logged_in = False

    def shutdown(self) -> None:
        self._logged_in = False

    @property
    def logged_in(self) -> bool:
        return self._logged_in


class _FakeBrokerFactory:
    """A concrete BrokerFactory implementation for testing."""

    def create_clients(
        self, symbols_path: str, broker_config: dict[str, Any]
    ) -> tuple[Any, Any]:
        md = _MockMarketDataProvider()
        order = _MockOrderExecutor()
        return md, order


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure registry is empty before and after each test."""
    saved = dict(_BROKER_REGISTRY)
    _BROKER_REGISTRY.clear()
    yield
    _BROKER_REGISTRY.clear()
    _BROKER_REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBrokerRegistration:
    """Tests for register_broker / get_broker_factory round-trip."""

    def test_register_and_retrieve(self) -> None:
        factory = _FakeBrokerFactory()
        register_broker("shioaji", factory)

        result = get_broker_factory("shioaji")
        assert result is factory

    def test_register_case_insensitive(self) -> None:
        factory = _FakeBrokerFactory()
        register_broker("Fubon", factory)

        assert get_broker_factory("fubon") is factory
        assert get_broker_factory("FUBON") is factory

    def test_unknown_broker_raises_value_error(self) -> None:
        register_broker("shioaji", _FakeBrokerFactory())

        with pytest.raises(ValueError, match=r"Unknown broker 'unknown'"):
            get_broker_factory("unknown")

    def test_error_message_lists_registered_brokers(self) -> None:
        register_broker("alpha", _FakeBrokerFactory())
        register_broker("beta", _FakeBrokerFactory())

        with pytest.raises(ValueError, match=r"\['alpha', 'beta'\]"):
            get_broker_factory("gamma")


class TestEnvVarSelection:
    """Tests for HFT_BROKER environment variable broker selection."""

    def test_default_broker_is_shioaji(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_BROKER", raising=False)
        factory = _FakeBrokerFactory()
        register_broker("shioaji", factory)

        result = get_broker_factory()
        assert result is factory

    def test_env_var_selects_broker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "fubon")
        sj_factory = _FakeBrokerFactory()
        fb_factory = _FakeBrokerFactory()
        register_broker("shioaji", sj_factory)
        register_broker("fubon", fb_factory)

        result = get_broker_factory()
        assert result is fb_factory

    def test_env_var_unknown_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "nonexistent")

        with pytest.raises(ValueError, match="nonexistent"):
            get_broker_factory()

    def test_switch_broker_via_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sj = _FakeBrokerFactory()
        fb = _FakeBrokerFactory()
        register_broker("shioaji", sj)
        register_broker("fubon", fb)

        monkeypatch.setenv("HFT_BROKER", "shioaji")
        assert get_broker_factory() is sj

        monkeypatch.setenv("HFT_BROKER", "fubon")
        assert get_broker_factory() is fb

    def test_explicit_name_overrides_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_BROKER", "fubon")
        sj = _FakeBrokerFactory()
        fb = _FakeBrokerFactory()
        register_broker("shioaji", sj)
        register_broker("fubon", fb)

        result = get_broker_factory("shioaji")
        assert result is sj


class TestListBrokers:
    """Tests for list_brokers."""

    def test_empty_registry(self) -> None:
        assert list_brokers() == []

    def test_returns_sorted(self) -> None:
        register_broker("fubon", _FakeBrokerFactory())
        register_broker("shioaji", _FakeBrokerFactory())
        register_broker("alpaca", _FakeBrokerFactory())

        assert list_brokers() == ["alpaca", "fubon", "shioaji"]


class TestProtocolConformance:
    """Verify mock objects satisfy the BrokerFactory protocol."""

    def test_fake_factory_is_broker_factory(self) -> None:
        factory = _FakeBrokerFactory()
        assert isinstance(factory, BrokerFactory)

    def test_factory_creates_client_pair(self) -> None:
        factory = _FakeBrokerFactory()
        md, order = factory.create_clients("symbols.yaml", {})

        assert hasattr(md, "subscribe_basket")
        assert hasattr(md, "fetch_snapshots")
        assert hasattr(md, "resubscribe")
        assert hasattr(md, "reload_symbols")
        assert hasattr(md, "validate_symbols")

        assert hasattr(order, "place_order")
        assert hasattr(order, "cancel_order")
        assert hasattr(order, "update_order")
        assert hasattr(order, "get_exchange")
        assert hasattr(order, "set_execution_callbacks")

    def test_mock_with_spec_satisfies_protocol_shape(self) -> None:
        md = MagicMock(spec=_MockMarketDataProvider)
        order = MagicMock(spec=_MockOrderExecutor)

        assert callable(md.subscribe_basket)
        assert callable(order.place_order)

    def test_account_provider_shape(self) -> None:
        provider = _MockAccountProvider()
        assert provider.get_positions() == []
        assert provider.get_account_balance() == 0
        assert provider.get_margin() == 0
        assert provider.list_position_detail() == []
        assert provider.list_profit_loss() == []

    def test_broker_session_shape(self) -> None:
        session = _MockBrokerSession()
        assert not session.logged_in
        session.login()
        assert session.logged_in
        session.close()
        assert not session.logged_in


class TestFactoryClientCreation:
    """Test that registered factory create_clients works end-to-end."""

    def test_registered_factory_creates_clients(self) -> None:
        factory = _FakeBrokerFactory()
        register_broker("test_broker", factory)

        retrieved = get_broker_factory("test_broker")
        md, order = retrieved.create_clients(
            "config/symbols.yaml", {"api_key": "test"}
        )

        assert md is not None
        assert order is not None
        assert md.fetch_snapshots() == []
        assert order.get_exchange("2330") == "TSE"

    def test_two_brokers_create_independent_clients(self) -> None:
        sj = _FakeBrokerFactory()
        fb = _FakeBrokerFactory()
        register_broker("shioaji", sj)
        register_broker("fubon", fb)

        md_sj, ord_sj = get_broker_factory("shioaji").create_clients("s.yaml", {})
        md_fb, ord_fb = get_broker_factory("fubon").create_clients("s.yaml", {})

        assert md_sj is not md_fb
        assert ord_sj is not ord_fb
