"""A HALT is one condition, not one condition per minute.

The engine logs a status line while StormGuard holds the system in HALT. Until
2026-09-09 every one of those lines was ``logger.error`` and carried no fields,
so one four-hour HALT on THESHOW wrote 239 of that day's 240 error records:

    16:59:28  StormGuard Transition  STORM -> HALT  reason=Drawdown -233bps
    16:59:28  System HALTED by StormGuard - blocking orders      <- error 1
    17:00:29  System HALTED by StormGuard - blocking orders      <- error 2
    ...       (237 more, one per minute, all identical)
    20:59:11  System HALTED by StormGuard - blocking orders      <- error 239
    21:00:04  StormGuard Transition  HALT -> NORMAL  reason=Recovery

    01:16:32  Mutating API call timed out ...                    <- error 240
              ^^ the only unrelated error of the day, 1 line in 240

Any error-rate signal built on that measures how long a HALT lasted, not how
many things went wrong.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.risk.storm_guard import StormGuard, StormGuardState
from hft_platform.services.system import HFTSystem


@pytest.fixture()
def system() -> HFTSystem:
    """An HFTSystem carrying only the HALT-log state the decision reads."""
    sysobj = HFTSystem.__new__(HFTSystem)
    sysobj._halt_log_mono = 0.0
    sysobj._halt_episode_start_mono = None
    return sysobj


def _run_halt(system: HFTSystem, duration_s: float, tick_s: float = 1.0) -> list[str]:
    """Tick a continuous HALT for ``duration_s`` and collect what it would log."""
    kinds = []
    t = 0.0
    while t <= duration_s:
        kind = system._halt_status_log_kind(t)
        if kind:
            kinds.append(kind)
        t += tick_s
    return kinds


class TestHaltStatusLogKind:
    def test_the_first_tick_of_a_halt_announces_the_episode(self, system: HFTSystem) -> None:
        assert system._halt_status_log_kind(1234.5) == "entry"

    def test_a_four_hour_halt_reports_one_error_not_two_hundred(self, system: HFTSystem) -> None:
        """The regression, at the length actually observed in production."""
        kinds = _run_halt(system, duration_s=4 * 60 * 60)

        assert kinds.count("entry") == 1, "a single HALT episode must announce itself once"
        assert kinds.count("heartbeat") == 240
        assert kinds[0] == "entry"
        assert set(kinds[1:]) == {"heartbeat"}

    def test_the_heartbeat_waits_for_the_full_interval(self, system: HFTSystem) -> None:
        assert system._halt_status_log_kind(100.0) == "entry"
        assert system._halt_status_log_kind(159.9) == ""
        assert system._halt_status_log_kind(160.0) == "heartbeat"
        assert system._halt_status_log_kind(219.0) == ""
        assert system._halt_status_log_kind(220.0) == "heartbeat"

    def test_a_second_halt_episode_announces_itself_again(self, system: HFTSystem) -> None:
        """De-escalation clears the episode, so the next HALT is an error again."""
        _run_halt(system, duration_s=600.0)

        # What the de-escalation branch does when StormGuard leaves HALT.
        system._halt_episode_start_mono = None

        assert system._halt_status_log_kind(5_000.0) == "entry"

    def test_a_halt_that_starts_at_clock_zero_is_still_one_episode(self, system: HFTSystem) -> None:
        """The "not halted" marker must not be a value the clock can hold.

        ``time.monotonic()`` is time since boot on Linux, so 0.0 is a reading,
        not an impossibility -- and a float sentinel inside the range of its own
        clock is exactly how the STORM entry stamp broke.
        """
        kinds = _run_halt(system, duration_s=300.0)

        assert kinds.count("entry") == 1
        assert kinds.count("heartbeat") == 5

    def test_the_reported_age_is_measured_from_the_start_of_the_episode(self, system: HFTSystem) -> None:
        """``halted_for_s`` is derived from this stamp, so it must not drift."""
        system._halt_status_log_kind(1_000.0)
        system._halt_status_log_kind(1_060.0)
        system._halt_status_log_kind(1_600.0)

        assert system._halt_episode_start_mono == 1_000.0


class TestStormGuardStateReason:
    def test_the_live_reason_is_readable_while_the_state_is_held(self) -> None:
        """The HALT status line needs the reason; the transition log is long gone."""
        metrics = MagicMock()
        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=metrics):
            guard = StormGuard()
        guard.metrics = metrics

        guard.update(drawdown_bps=-233)

        assert guard.state is StormGuardState.HALT
        assert guard.state_reason == "Drawdown -233bps"
