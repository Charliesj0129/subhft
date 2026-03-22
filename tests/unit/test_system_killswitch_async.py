"""Tests for A1 audit fix: kill-switch file IO must not block event loop."""

from __future__ import annotations

import inspect

import pytest


def test_supervise_uses_run_in_executor_for_kill_switch():
    """Kill-switch file IO must use run_in_executor, not direct sync calls."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)

    assert "run_in_executor" in source, (
        "_supervise() should use run_in_executor for kill-switch file IO"
    )


def test_read_kill_switch_reason_helper_exists():
    """Module-level _read_kill_switch_reason helper must exist for executor offloading."""
    from hft_platform.services import system

    assert hasattr(system, "_read_kill_switch_reason"), (
        "Missing _read_kill_switch_reason helper for run_in_executor offloading"
    )
    assert callable(system._read_kill_switch_reason)
