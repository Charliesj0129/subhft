"""Tests for shadow mode bootstrap dual-lock validation."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


def test_shadow_mode_with_live_orders_refuses_start():
    env = {"HFT_ORDER_SHADOW_MODE": "1", "HFT_ORDER_MODE": "live", "HFT_MODE": "real"}
    with patch.dict(os.environ, env, clear=False):
        from hft_platform.services.bootstrap import validate_shadow_lock

        with pytest.raises(SystemExit):
            validate_shadow_lock()


def test_shadow_mode_with_real_orders_refuses_start():
    env = {"HFT_ORDER_SHADOW_MODE": "1", "HFT_ORDER_MODE": "real", "HFT_MODE": "real"}
    with patch.dict(os.environ, env, clear=False):
        from hft_platform.services.bootstrap import validate_shadow_lock

        with pytest.raises(SystemExit):
            validate_shadow_lock()


def test_shadow_mode_with_sim_orders_passes():
    env = {"HFT_ORDER_SHADOW_MODE": "1", "HFT_ORDER_MODE": "sim", "HFT_MODE": "sim"}
    with patch.dict(os.environ, env, clear=False):
        from hft_platform.services.bootstrap import validate_shadow_lock

        validate_shadow_lock()  # Should not raise


def test_no_shadow_mode_skips_check():
    env = {"HFT_ORDER_SHADOW_MODE": "0", "HFT_ORDER_MODE": "live", "HFT_MODE": "real"}
    with patch.dict(os.environ, env, clear=False):
        from hft_platform.services.bootstrap import validate_shadow_lock

        validate_shadow_lock()  # Should not raise


def test_shadow_mode_unset_skips_check():
    # When HFT_ORDER_SHADOW_MODE is not set at all, should also pass
    env = {"HFT_ORDER_MODE": "live", "HFT_MODE": "real"}
    # Remove HFT_ORDER_SHADOW_MODE if present
    patched_env = {k: v for k, v in os.environ.items() if k != "HFT_ORDER_SHADOW_MODE"}
    patched_env.update(env)
    with patch.dict(os.environ, patched_env, clear=True):
        from hft_platform.services.bootstrap import validate_shadow_lock

        validate_shadow_lock()  # Should not raise
