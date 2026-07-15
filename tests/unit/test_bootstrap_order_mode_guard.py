"""Tests for HFT_ORDER_MODE safety guard in bootstrap."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from hft_platform.services.bootstrap import resolve_order_mode, validate_order_mode_safety


def test_live_order_mode_requires_real_hft_mode():
    env = {"HFT_MODE": "sim", "HFT_ORDER_MODE": "live"}
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(SystemExit):
            validate_order_mode_safety()


def test_live_order_mode_accepted_with_real_hft_mode():
    env = {"HFT_MODE": "real", "HFT_ORDER_MODE": "live", "HFT_LIVE_CONFIRM": "yes-i-know"}
    with patch.dict(os.environ, env, clear=False):
        result = validate_order_mode_safety()
    assert result is None


def test_live_order_mode_accepted_with_live_hft_mode():
    env = {"HFT_MODE": "live", "HFT_ORDER_MODE": "live", "HFT_LIVE_CONFIRM": "yes-i-know"}
    with patch.dict(os.environ, env, clear=False):
        result = validate_order_mode_safety()
    assert result is None


def test_sim_order_mode_always_accepted():
    for mode in ("sim", "real", "replay", ""):
        env = {"HFT_MODE": mode, "HFT_ORDER_MODE": "sim"}
        with patch.dict(os.environ, env, clear=False):
            result = validate_order_mode_safety()
        assert result is None


def test_disabled_order_mode_is_accepted_without_live_confirmation():
    env = {"HFT_MODE": "live", "HFT_ORDER_MODE": "disabled"}
    with patch.dict(os.environ, env, clear=False):
        result = validate_order_mode_safety()
    assert result is None


@pytest.mark.parametrize("order_mode", ["", "disable", "typo"])
def test_unknown_order_mode_refuses_start(order_mode):
    env = {"HFT_MODE": "sim", "HFT_ORDER_MODE": order_mode}
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(SystemExit, match="Unsupported HFT_ORDER_MODE"):
            validate_order_mode_safety()


def test_real_hft_mode_normalized_to_live_in_environ():
    env = {"HFT_MODE": "real", "HFT_ORDER_MODE": "sim"}
    with patch.dict(os.environ, env, clear=False):
        validate_order_mode_safety()
        assert os.environ["HFT_MODE"] == "live"


def test_real_order_mode_normalized_to_live_in_environ():
    env = {"HFT_MODE": "live", "HFT_ORDER_MODE": "real", "HFT_LIVE_CONFIRM": "yes-i-know"}
    with patch.dict(os.environ, env, clear=False):
        validate_order_mode_safety()
        assert os.environ["HFT_ORDER_MODE"] == "live"


def test_legacy_order_simulation_true_maps_to_sim_when_primary_mode_is_absent():
    env = {"HFT_MODE": "sim", "HFT_ORDER_SIMULATION": "1"}
    with patch.dict(os.environ, env, clear=True):
        assert resolve_order_mode() == "sim"


def test_legacy_order_simulation_false_maps_to_live_and_keeps_live_guard():
    env = {"HFT_MODE": "sim", "HFT_ORDER_SIMULATION": "0"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit, match="with HFT_MODE=sim is invalid"):
            validate_order_mode_safety()
        assert os.environ["HFT_ORDER_MODE"] == "live"


@pytest.mark.parametrize("legacy_value", ["", "maybe", "disabled"])
def test_unknown_legacy_order_simulation_value_refuses_start(legacy_value):
    env = {"HFT_MODE": "live", "HFT_LIVE_CONFIRM": "YES_I_WANT_LIVE", "HFT_ORDER_SIMULATION": legacy_value}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit, match="Unsupported HFT_ORDER_SIMULATION"):
            resolve_order_mode()
