from __future__ import annotations

from typing import Any

import pytest

from hft_platform.broker.factory import BrokerFactory, register_broker


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Ensure each test starts and ends with a clean registry."""
    BrokerFactory.clear()
    yield
    BrokerFactory.clear()


def _mock_constructor(config: dict[str, Any]) -> dict[str, Any]:
    return {"broker": "mock", **config}


def _another_constructor(config: dict[str, Any]) -> dict[str, Any]:
    return {"broker": "another", **config}


class TestBrokerFactory:
    def test_register_and_create(self) -> None:
        BrokerFactory.register("mock", _mock_constructor)
        result = BrokerFactory.create("mock", {"key": "val"})
        assert result == {"broker": "mock", "key": "val"}

    def test_create_unknown_broker_raises(self) -> None:
        BrokerFactory.register("alpha", _mock_constructor)
        with pytest.raises(ValueError, match="Unknown broker 'missing'") as exc_info:
            BrokerFactory.create("missing", {})
        assert "alpha" in str(exc_info.value)

    def test_create_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        BrokerFactory.register("fubon", _another_constructor)
        monkeypatch.setenv("HFT_BROKER", "fubon")
        result = BrokerFactory.create_from_env({"x": 1})
        assert result == {"broker": "another", "x": 1}

    def test_create_from_env_default_shioaji(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        BrokerFactory.register("shioaji", _mock_constructor)
        monkeypatch.delenv("HFT_BROKER", raising=False)
        result = BrokerFactory.create_from_env({})
        assert result == {"broker": "mock"}

    def test_available_returns_sorted_list(self) -> None:
        BrokerFactory.register("zebra", _mock_constructor)
        BrokerFactory.register("alpha", _mock_constructor)
        BrokerFactory.register("middle", _mock_constructor)
        assert BrokerFactory.available() == ["alpha", "middle", "zebra"]

    def test_is_registered(self) -> None:
        assert BrokerFactory.is_registered("mock") is False
        BrokerFactory.register("mock", _mock_constructor)
        assert BrokerFactory.is_registered("mock") is True

    def test_clear_removes_all(self) -> None:
        BrokerFactory.register("a", _mock_constructor)
        BrokerFactory.register("b", _mock_constructor)
        assert len(BrokerFactory.available()) == 2
        BrokerFactory.clear()
        assert BrokerFactory.available() == []

    def test_register_overwrites(self) -> None:
        BrokerFactory.register("dup", _mock_constructor)
        BrokerFactory.register("dup", _another_constructor)
        result = BrokerFactory.create("dup", {"v": 99})
        assert result == {"broker": "another", "v": 99}

    def test_register_broker_decorator(self) -> None:
        @register_broker("decorated")
        def create_decorated(config: dict[str, Any]) -> str:
            return f"decorated-{config.get('id', 0)}"

        assert BrokerFactory.is_registered("decorated")
        result = BrokerFactory.create("decorated", {"id": 42})
        assert result == "decorated-42"
