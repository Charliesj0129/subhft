"""Tests for src/hft_platform/services/registry.py."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from hft_platform.services.registry import ServiceRegistry

REQUIRED_FIELD_NAMES = [
    "settings",
    "bus",
    "raw_queue",
    "raw_exec_queue",
    "risk_queue",
    "order_queue",
    "recorder_queue",
    "position_store",
    "order_id_map",
    "storm_guard",
    "symbol_metadata",
    "price_scale_provider",
    "broker_id",
    "md_client",
    "order_client",
    "client",
    "md_service",
    "feature_engine",
    "order_adapter",
    "execution_gateway",
    "exec_service",
    "risk_engine",
    "recon_service",
    "strategy_runner",
    "recorder",
]

OPTIONAL_FIELD_NAMES = [
    "feature_profile_registry",
    "feature_profile",
    "feature_rollout_controller",
    "feature_rollout_assignment",
    "gateway_service",
    "intent_channel",
]

EXPECTED_TOTAL_FIELDS = 31


def _required_kwargs() -> dict[str, object]:
    """Return keyword arguments for all required (non-default) fields."""
    return {name: MagicMock(name=name) for name in REQUIRED_FIELD_NAMES}


def _full_kwargs() -> dict[str, object]:
    """Return keyword arguments for every field (required + optional)."""
    kwargs = _required_kwargs()
    for name in OPTIONAL_FIELD_NAMES:
        kwargs[name] = MagicMock(name=name)
    return kwargs


# ---------------------------------------------------------------------------
# 1. __slots__
# ---------------------------------------------------------------------------


class TestServiceRegistrySlots:
    """Verify __slots__ on ServiceRegistry (HFT performance requirement)."""

    def test_slots_defined(self) -> None:
        assert hasattr(ServiceRegistry, "__slots__")

    def test_no_instance_dict(self) -> None:
        reg = ServiceRegistry(**_required_kwargs())
        assert not hasattr(reg, "__dict__")


# ---------------------------------------------------------------------------
# 2. Required fields raise on missing
# ---------------------------------------------------------------------------


class TestRequiredFieldsRaiseOnMissing:
    """Constructing without required fields must raise TypeError."""

    def test_no_args_raises(self) -> None:
        with pytest.raises(TypeError):
            ServiceRegistry()  # type: ignore[call-arg]

    @pytest.mark.parametrize("field_name", REQUIRED_FIELD_NAMES)
    def test_each_required_field_missing_raises(self, field_name: str) -> None:
        kwargs = _required_kwargs()
        del kwargs[field_name]
        with pytest.raises(TypeError):
            ServiceRegistry(**kwargs)


# ---------------------------------------------------------------------------
# 3. Optional fields default to None
# ---------------------------------------------------------------------------


class TestOptionalFieldsDefaultToNone:
    """Optional fields must default to None when not supplied."""

    @pytest.mark.parametrize("field_name", OPTIONAL_FIELD_NAMES)
    def test_default_is_none(self, field_name: str) -> None:
        reg = ServiceRegistry(**_required_kwargs())
        assert getattr(reg, field_name) is None

    def test_all_optional_fields_none_together(self) -> None:
        reg = ServiceRegistry(**_required_kwargs())
        for name in OPTIONAL_FIELD_NAMES:
            assert getattr(reg, name) is None, f"{name} should default to None"


# ---------------------------------------------------------------------------
# 4. Construction with all fields
# ---------------------------------------------------------------------------


class TestConstructionWithAllFields:
    """Can construct with every field populated."""

    def test_all_fields_stored(self) -> None:
        kwargs = _full_kwargs()
        reg = ServiceRegistry(**kwargs)
        for name, value in kwargs.items():
            assert getattr(reg, name) is value, f"{name} not stored correctly"

    def test_required_fields_stored(self) -> None:
        kwargs = _required_kwargs()
        reg = ServiceRegistry(**kwargs)
        for name, value in kwargs.items():
            assert getattr(reg, name) is value

    def test_optional_field_can_be_set(self) -> None:
        kwargs = _required_kwargs()
        sentinel = MagicMock(name="gateway_sentinel")
        kwargs["gateway_service"] = sentinel
        reg = ServiceRegistry(**kwargs)
        assert reg.gateway_service is sentinel


# ---------------------------------------------------------------------------
# 5. Field count guard
# ---------------------------------------------------------------------------


class TestFieldCount:
    """Guard against accidental field addition or removal."""

    def test_field_count_matches(self) -> None:
        fields = dataclasses.fields(ServiceRegistry)
        assert len(fields) == EXPECTED_TOTAL_FIELDS

    def test_required_plus_optional_equals_total(self) -> None:
        assert len(REQUIRED_FIELD_NAMES) + len(OPTIONAL_FIELD_NAMES) == EXPECTED_TOTAL_FIELDS


# ---------------------------------------------------------------------------
# 6. Mutability check
# ---------------------------------------------------------------------------


class TestMutability:
    """ServiceRegistry is a mutable (non-frozen) dataclass."""

    def test_field_assignment_allowed(self) -> None:
        reg = ServiceRegistry(**_required_kwargs())
        new_mock = MagicMock(name="new_gateway")
        reg.gateway_service = new_mock
        assert reg.gateway_service is new_mock

    def test_required_field_reassignment(self) -> None:
        reg = ServiceRegistry(**_required_kwargs())
        new_bus = MagicMock(name="new_bus")
        reg.bus = new_bus
        assert reg.bus is new_bus

    def test_not_frozen(self) -> None:
        """Assigning to a frozen dataclass raises FrozenInstanceError; this must not."""
        reg = ServiceRegistry(**_required_kwargs())
        reg.broker_id = "fubon"
        assert reg.broker_id == "fubon"
