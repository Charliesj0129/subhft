"""Tests for HFT_ORDER_MODE safety guard in bootstrap."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from hft_platform.services.bootstrap import validate_order_mode_safety


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
