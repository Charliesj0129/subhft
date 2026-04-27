"""Tests for A1 audit fix: kill-switch file IO must not block event loop."""

from __future__ import annotations

import inspect


def test_supervise_uses_run_in_executor_for_kill_switch():
    """Kill-switch file IO must use run_in_executor, not direct sync calls."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)

    assert "run_in_executor" in source, "_supervise() should use run_in_executor for kill-switch file IO"


def test_read_kill_switch_reason_helper_exists():
    """Module-level _read_kill_switch_reason helper must exist for executor offloading."""
    from hft_platform.services import system

    assert hasattr(system, "_read_kill_switch_reason"), (
        "Missing _read_kill_switch_reason helper for run_in_executor offloading"
    )
    assert callable(system._read_kill_switch_reason)


def test_supervise_checks_kill_switch_file_independently_of_redis() -> None:
    """P2-e (2026-04-27): the file-based kill switch must run on every
    supervise tick regardless of Redis state. Verify by source inspection
    that the file check appears BEFORE the Redis-keyed check, so a Redis
    outage cannot skip the file path."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)

    # File check
    file_idx = source.find("HFT_KILL_SWITCH_PATH")
    # Redis-keyed check
    redis_idx = source.find("hft:emergency_halt")

    assert file_idx != -1, "kill-switch file check missing from _supervise"
    assert redis_idx != -1, "Redis emergency-halt check missing from _supervise"
    assert file_idx < redis_idx, (
        "file-based kill switch must be checked BEFORE the Redis-keyed halt "
        "(otherwise a Redis outage could mask a kill-switch file)"
    )


def test_supervise_does_not_misleadingly_call_redis_failure_a_fallback() -> None:
    """P2-e: The previous comment in _supervise's Redis except-handler said
    'Redis unavailable — fall back to file-based kill switch', but the file
    check is unconditional and not actually a 'fallback'. Pin the new wording
    so future readers don't reintroduce the confusion."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)
    assert "fall back to file-based kill switch" not in source, (
        "Misleading 'fallback' comment must be removed (P2-e)"
    )
