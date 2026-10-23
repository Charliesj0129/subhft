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


def test_supervise_reads_the_kill_switch_file_and_no_redis_key() -> None:
    """The kill-switch file is the one remote stop ``_supervise`` reads.

    It used to also read an ``hft:emergency_halt`` Redis key through a client
    nothing assigned, so the read never ran and Telegram ``/stop`` halted
    nothing. ``/stop`` now writes the same file (``bot/handlers.py::cmd_stop``);
    a second, Redis-keyed channel must not come back.
    """
    from hft_platform.risk import kill_switch
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)

    assert "kill_switch_path" in source, "kill-switch file check missing from _supervise"
    assert "hft:emergency_halt" not in source
    assert "_redis_client" not in source
    # The operator override, owned by the shared module: losing it would make
    # the path unoverridable in a container where .state/ is not mounted.
    assert kill_switch.PATH_ENV == "HFT_KILL_SWITCH_PATH"


def test_supervise_does_not_misleadingly_call_redis_failure_a_fallback() -> None:
    """P2-e: The previous comment in _supervise's Redis except-handler said
    'Redis unavailable — fall back to file-based kill switch', but the file
    check is unconditional and not actually a 'fallback'. Pin the new wording
    so future readers don't reintroduce the confusion."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)
    assert "fall back to file-based kill switch" not in source, "Misleading 'fallback' comment must be removed (P2-e)"
