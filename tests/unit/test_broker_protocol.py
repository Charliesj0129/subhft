from __future__ import annotations

from typing import Any, Callable

import pytest

from hft_platform.broker.protocol import (
    FUBON_CAPABILITIES,
    SHIOAJI_CAPABILITIES,
    BrokerCapabilities,
    BrokerProtocol,
)


class _FakeBroker:
    """Minimal implementation satisfying BrokerProtocol."""

    __slots__ = ("_logged_in",)

    def __init__(self) -> None:
        self._logged_in = False

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def login(self, *args: Any, **kwargs: Any) -> bool:
        self._logged_in = True
        return True

    def reconnect(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def close(self, logout: bool = False) -> None:
        self._logged_in = False

    def shutdown(self, logout: bool = False) -> None:
        self.close(logout=logout)

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        pass

    def fetch_snapshots(self) -> list[Any]:
        return []

    def reload_symbols(self) -> None:
        pass

    def resubscribe(self) -> bool:
        return True

    def get_exchange(self, symbol: str) -> str:
        return "TSE"

    def set_execution_callbacks(
        self, on_order: Callable[..., Any], on_deal: Callable[..., Any]
    ) -> None:
        pass

    def place_order(self, **kwargs: Any) -> Any:
        return {"status": "ok"}

    def cancel_order(self, trade: Any) -> Any:
        return {"status": "cancelled"}

    def update_order(
        self, trade: Any, price: float | None = None, qty: int | None = None
    ) -> Any:
        return {"status": "updated"}

    def get_positions(self) -> list[Any]:
        return []

    def get_account_balance(self, account: Any = None) -> Any:
        return {}

    def get_margin(self, account: Any = None) -> Any:
        return {}

    def list_position_detail(self, account: Any = None) -> list[Any]:
        return []

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        return []

    def validate_symbols(self) -> list[str]:
        return ["2330"]

    def get_contract_refresh_status(self) -> dict[str, Any]:
        return {"refreshed": True}


class _IncompleteBroker:
    """Missing most protocol methods -- should NOT satisfy BrokerProtocol."""

    @property
    def logged_in(self) -> bool:
        return False

    def login(self, *args: Any, **kwargs: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestBrokerProtocolConformance:
    def test_fake_broker_satisfies_protocol(self) -> None:
        broker = _FakeBroker()
        assert isinstance(broker, BrokerProtocol)

    def test_incomplete_broker_does_not_satisfy_protocol(self) -> None:
        incomplete = _IncompleteBroker()
        assert not isinstance(incomplete, BrokerProtocol)

    def test_fake_broker_login_works(self) -> None:
        broker = _FakeBroker()
        assert not broker.logged_in
        assert broker.login() is True
        assert broker.logged_in

    def test_fake_broker_shutdown_clears_login(self) -> None:
        broker = _FakeBroker()
        broker.login()
        broker.shutdown(logout=True)
        assert not broker.logged_in


# ---------------------------------------------------------------------------
# BrokerCapabilities
# ---------------------------------------------------------------------------


class TestBrokerCapabilities:
    def test_frozen(self) -> None:
        cap = BrokerCapabilities(name="test")
        with pytest.raises(AttributeError):
            cap.name = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(BrokerCapabilities, "__slots__")
        cap = BrokerCapabilities(name="test")
        with pytest.raises(AttributeError):
            cap.__dict__  # noqa: B018

    def test_defaults(self) -> None:
        cap = BrokerCapabilities(name="default")
        assert cap.supports_batch_order is False
        assert cap.supports_smart_order is False
        assert cap.supports_l2_depth is True
        assert cap.max_custom_field_len == 6
        assert cap.auth_method == "cert"
        assert cap.max_rate_per_second == 20

    def test_shioaji_capabilities(self) -> None:
        assert SHIOAJI_CAPABILITIES.name == "shioaji"
        assert SHIOAJI_CAPABILITIES.supports_batch_order is False
        assert SHIOAJI_CAPABILITIES.auth_method == "cert"
        assert SHIOAJI_CAPABILITIES.max_rate_per_second == 25

    def test_fubon_capabilities(self) -> None:
        assert FUBON_CAPABILITIES.name == "fubon"
        assert FUBON_CAPABILITIES.supports_batch_order is True
        assert FUBON_CAPABILITIES.supports_smart_order is True
        assert FUBON_CAPABILITIES.auth_method == "apikey"
        assert FUBON_CAPABILITIES.max_rate_per_second == 15
        assert FUBON_CAPABILITIES.max_custom_field_len == 32
