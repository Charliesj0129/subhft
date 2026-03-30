"""Electronic Eye: automated TXO options market-making with delta-neutral hedging."""
from __future__ import annotations

import enum

from structlog import get_logger

logger = get_logger("strategy.electronic_eye")


class EyeState(enum.IntEnum):
    INIT = 0
    QUOTING = 1
    NARROW = 2
    RESTRICT = 3
    HALT = 4


class Guardian:
    """Risk state machine for the Electronic Eye strategy.

    State transitions:
        INIT -> QUOTING: via activate()
        QUOTING -> NARROW: utilization > warn_pct
        NARROW -> QUOTING: utilization <= warn_pct AND stress OK
        QUOTING/NARROW -> RESTRICT: greeks rejection or stress failure
        RESTRICT -> QUOTING: stress OK AND utilization OK
        ANY -> HALT: on_halt() (terminal, no recovery)
    """

    __slots__ = (
        "state",
        "_warn_pct",
        "_max_worst_pnl",
        "_stress_interval_s",
        "_last_stress_ok",
        "_last_util_ok",
    )

    def __init__(
        self,
        warn_utilization_pct: float = 80.0,
        stress_interval_s: int = 60,
        max_worst_case_pnl_ntd: int = -500_000,
    ) -> None:
        self.state = EyeState.INIT
        self._warn_pct = warn_utilization_pct
        self._max_worst_pnl = max_worst_case_pnl_ntd
        self._stress_interval_s = stress_interval_s
        self._last_stress_ok = True
        self._last_util_ok = True

    def activate(self) -> None:
        """Transition INIT -> QUOTING to begin market-making."""
        if self.state == EyeState.INIT:
            self.state = EyeState.QUOTING
            logger.info("guardian_activated", state="QUOTING")

    def on_utilization(self, pct: float) -> None:
        """Update risk state based on current margin utilization percentage."""
        self._last_util_ok = pct <= self._warn_pct
        self._reevaluate()

    def on_greeks_rejection(self, reason: str) -> None:
        """Transition to RESTRICT when a greeks-limit breach is detected."""
        if self.state in (EyeState.QUOTING, EyeState.NARROW):
            self.state = EyeState.RESTRICT
            logger.warning("guardian_restrict", reason=reason)

    def on_stress_result(self, within_limits: bool, worst_pnl: float) -> None:
        """Update risk state based on the latest stress-test result."""
        self._last_stress_ok = within_limits
        if not within_limits and self.state in (EyeState.QUOTING, EyeState.NARROW):
            self.state = EyeState.RESTRICT
            logger.warning("guardian_restrict_stress", worst_pnl=worst_pnl)
        elif within_limits:
            self._reevaluate()

    def on_halt(self) -> None:
        """Transition to HALT (terminal). Requires manual intervention to recover."""
        if self.state != EyeState.HALT:
            self.state = EyeState.HALT
            logger.error("guardian_halt")

    def allows_new_quotes(self) -> bool:
        """Return True if new quote orders may be submitted."""
        return self.state in (EyeState.QUOTING, EyeState.NARROW)

    def should_flatten(self) -> bool:
        """Return True if all positions must be flattened immediately."""
        return self.state == EyeState.HALT

    def _reevaluate(self) -> None:
        """Internal: recompute state from cached stress and utilization flags."""
        if self.state == EyeState.HALT:
            return
        if self._last_stress_ok and self._last_util_ok:
            if self.state in (EyeState.NARROW, EyeState.RESTRICT):
                self.state = EyeState.QUOTING
                logger.info("guardian_clear", state="QUOTING")
        elif not self._last_util_ok:
            if self.state == EyeState.QUOTING:
                self.state = EyeState.NARROW
                logger.info("guardian_narrow")
