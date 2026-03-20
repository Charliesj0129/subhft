"""Tests for src/hft_platform/services/registry.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.services.registry import ServiceRegistry


def _required_kwargs() -> dict[str, MagicMock]:
    """Return keyword arguments for all required (non-default) fields."""
    return {
        "settings": MagicMock(),
        "bus": MagicMock(),
        "raw_queue": MagicMock(),
        "raw_exec_queue": MagicMock(),
        "risk_queue": MagicMock(),
        "order_queue": MagicMock(),
        "recorder_queue": MagicMock(),
        "position_store": MagicMock(),
        "order_id_map": MagicMock(),
        "storm_guard": MagicMock(),
        "symbol_metadata": MagicMock(),
        "price_scale_provider": MagicMock(),
        "broker_id": MagicMock(),
        "md_client": MagicMock(),
        "order_client": MagicMock(),
        "client": MagicMock(),
        "md_service": MagicMock(),
        "feature_engine": MagicMock(),
        "order_adapter": MagicMock(),
        "execution_gateway": MagicMock(),
        "exec_service": MagicMock(),
        "risk_engine": MagicMock(),
        "recon_service": MagicMock(),
        "strategy_runner": MagicMock(),
        "recorder": MagicMock(),
    }


class TestServiceRegistrySlots:
    """Verify __slots__ on ServiceRegistry."""

    def test_has_slots(self) -> None:
        assert hasattr(ServiceRegistry, "__slots__")

    def test_no_instance_dict(self) -> None:
        reg = ServiceRegistry(**_required_kwargs())
        assert not hasattr(reg, "__dict__")


class TestServiceRegistryConstruction:
    """Construction with required and optional fields."""

    def test_required_fields(self) -> None:
        kwargs = _required_kwargs()
        reg = ServiceRegistry(**kwargs)
        for name, mock in kwargs.items():
            assert getattr(reg, name) is mock

    def test_optional_fields_default_none(self) -> None:
        reg = ServiceRegistry(**_required_kwargs())
        optional_fields = [
            "feature_profile_registry",
            "feature_profile",
            "feature_rollout_controller",
            "feature_rollout_assignment",
            "gateway_service",
            "intent_channel",
        ]
        for field_name in optional_fields:
            assert getattr(reg, field_name) is None, f"{field_name} should default to None"

    def test_construction_error_on_missing_field(self) -> None:
        kwargs = _required_kwargs()
        del kwargs["bus"]
        with pytest.raises(TypeError):
            ServiceRegistry(**kwargs)

    def test_optional_field_can_be_set(self) -> None:
        kwargs = _required_kwargs()
        gw = MagicMock()
        kwargs["gateway_service"] = gw
        reg = ServiceRegistry(**kwargs)
        assert reg.gateway_service is gw
