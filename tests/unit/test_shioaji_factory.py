"""Tests for ShioajiBrokerFactory and auto-registration."""
from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest

from hft_platform.feed_adapter.broker_registry import (
    BrokerFactory,
    get_broker_factory,
    register_broker,
)
from hft_platform.feed_adapter.shioaji.factory import ShioajiBrokerFactory

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestBrokerFactoryProtocol:
    def test_shioaji_factory_satisfies_protocol(self) -> None:
        factory = ShioajiBrokerFactory()
        assert isinstance(factory, BrokerFactory)

    def test_has_slots(self) -> None:
        assert hasattr(ShioajiBrokerFactory, "__slots__")


# ---------------------------------------------------------------------------
# create_clients basics
# ---------------------------------------------------------------------------

class TestCreateClients:
    def test_returns_tuple_of_two(self) -> None:
        with patch(
            "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
        ) as mock_facade:
            factory = ShioajiBrokerFactory()
            md, order = factory.create_clients("/tmp/symbols.yaml", {"simulation": True})

            assert mock_facade.call_count == 2
            assert md is mock_facade.return_value
            assert order is mock_facade.return_value

    def test_md_config_not_mutated(self) -> None:
        """Market-data config must not gain order-side overrides."""
        with patch(
            "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
        ) as mock_facade:
            original_cfg: dict[str, object] = {"api_key": "k"}
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", original_cfg)

            # First call = md client, should get a copy of original
            md_call_cfg = mock_facade.call_args_list[0][0][1]
            assert md_call_cfg == {"api_key": "k"}
            # Original must be untouched
            assert original_cfg == {"api_key": "k"}


# ---------------------------------------------------------------------------
# Environment variable overrides (parity with bootstrap._build_broker_clients)
# ---------------------------------------------------------------------------

class TestOrderEnvOverrides:
    def test_hft_order_mode_sim(self) -> None:
        with (
            patch.dict(os.environ, {"HFT_ORDER_MODE": "sim"}, clear=False),
            patch(
                "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
            ) as mock_facade,
        ):
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", {})

            order_cfg = mock_facade.call_args_list[1][0][1]
            assert order_cfg["simulation"] is True
            assert order_cfg["activate_ca"] is False

    def test_hft_order_mode_paper(self) -> None:
        with (
            patch.dict(os.environ, {"HFT_ORDER_MODE": "paper"}, clear=False),
            patch(
                "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
            ) as mock_facade,
        ):
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", {})

            order_cfg = mock_facade.call_args_list[1][0][1]
            assert order_cfg["simulation"] is True

    def test_hft_order_mode_live_no_simulation(self) -> None:
        with (
            patch.dict(os.environ, {"HFT_ORDER_MODE": "live"}, clear=False),
            patch(
                "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
            ) as mock_facade,
        ):
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", {})

            order_cfg = mock_facade.call_args_list[1][0][1]
            assert order_cfg["simulation"] is False

    def test_hft_order_simulation_flag(self) -> None:
        env = {"HFT_ORDER_SIMULATION": "1"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
            ) as mock_facade,
        ):
            # Ensure ORDER_MODE doesn't interfere
            os.environ.pop("HFT_ORDER_MODE", None)
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", {})

            order_cfg = mock_facade.call_args_list[1][0][1]
            assert order_cfg["simulation"] is True
            assert order_cfg["activate_ca"] is False

    def test_hft_order_simulation_flag_sim_string(self) -> None:
        env = {"HFT_ORDER_SIMULATION": "sim"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
            ) as mock_facade,
        ):
            os.environ.pop("HFT_ORDER_MODE", None)
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", {})

            order_cfg = mock_facade.call_args_list[1][0][1]
            assert order_cfg["simulation"] is True

    def test_hft_order_no_ca(self) -> None:
        env = {"HFT_ORDER_NO_CA": "1"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
            ) as mock_facade,
        ):
            os.environ.pop("HFT_ORDER_MODE", None)
            os.environ.pop("HFT_ORDER_SIMULATION", None)
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", {})

            order_cfg = mock_facade.call_args_list[1][0][1]
            assert order_cfg["activate_ca"] is False

    def test_order_mode_takes_precedence_over_simulation_flag(self) -> None:
        """HFT_ORDER_MODE has higher priority than HFT_ORDER_SIMULATION."""
        env = {"HFT_ORDER_MODE": "live", "HFT_ORDER_SIMULATION": "1"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
            ) as mock_facade,
        ):
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", {})

            order_cfg = mock_facade.call_args_list[1][0][1]
            # ORDER_MODE=live wins → simulation=False
            assert order_cfg["simulation"] is False

    def test_no_env_vars_no_override(self) -> None:
        with (
            patch.dict(
                os.environ,
                {},
                clear=False,
            ),
            patch(
                "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
            ) as mock_facade,
        ):
            os.environ.pop("HFT_ORDER_MODE", None)
            os.environ.pop("HFT_ORDER_SIMULATION", None)
            os.environ.pop("HFT_ORDER_NO_CA", None)
            factory = ShioajiBrokerFactory()
            factory.create_clients("/tmp/s.yaml", {"api_key": "x"})

            order_cfg = mock_facade.call_args_list[1][0][1]
            assert "simulation" not in order_cfg
            assert "activate_ca" not in order_cfg


# ---------------------------------------------------------------------------
# Broker registry helpers
# ---------------------------------------------------------------------------

class TestBrokerRegistry:
    def test_register_and_get(self) -> None:
        factory = ShioajiBrokerFactory()
        register_broker("test_shioaji", factory)
        assert get_broker_factory("test_shioaji") is factory

    def test_get_missing_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="no_such_broker"):
            get_broker_factory("no_such_broker")


# ---------------------------------------------------------------------------
# Auto-registration via __init__.py import
# ---------------------------------------------------------------------------

class TestAutoRegistration:
    def test_shioaji_auto_registered(self) -> None:
        # Force re-import to trigger auto-registration
        import hft_platform.feed_adapter.shioaji as shioaji_pkg

        importlib.reload(shioaji_pkg)

        factory = get_broker_factory("shioaji")
        assert isinstance(factory, ShioajiBrokerFactory)
