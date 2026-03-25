"""Session lifecycle governor — manages trading session phases and track gates.

SessionPhase tracks the current market session (PRE_OPEN, OPEN, CLOSE_ONLY,
CLOSED). TrackGate controls whether strategies are allowed to generate new
intents based on phase + autonomy state.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("ops.session_governor")


class SessionPhase(StrEnum):
    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    CLOSE_ONLY = "CLOSE_ONLY"
    CLOSED = "CLOSED"


class TrackGate(IntEnum):
    """Gate levels — higher value = more restrictive."""

    OPEN = 0
    REDUCE_ONLY = 1
    CLOSE_ONLY = 2
    LOCKED = 3


_PHASE_TO_GATE: dict[SessionPhase, TrackGate] = {
    SessionPhase.PRE_OPEN: TrackGate.LOCKED,
    SessionPhase.OPEN: TrackGate.OPEN,
    SessionPhase.CLOSE_ONLY: TrackGate.CLOSE_ONLY,
    SessionPhase.CLOSED: TrackGate.LOCKED,
}


class SessionGovernor:
    """Governs the trading session lifecycle and track gate state.

    The effective gate is the *maximum* of the phase-derived gate and any
    externally imposed override (e.g. autonomy reduce-only).
    """

    __slots__ = (
        "_phase",
        "_override_gate",
        "_effective_gate",
        "_last_phase_change_ns",
        "_on_phase_change",
    )

    def __init__(self, on_phase_change: Callable[[SessionPhase], Any] | None = None) -> None:
        self._phase: SessionPhase = SessionPhase.PRE_OPEN
        self._override_gate: TrackGate = TrackGate.OPEN
        self._effective_gate: TrackGate = TrackGate.LOCKED
        self._last_phase_change_ns: int = timebase.now_ns()
        self._on_phase_change = on_phase_change

    # ---- Phase management ----

    @property
    def phase(self) -> SessionPhase:
        return self._phase

    @property
    def effective_gate(self) -> TrackGate:
        return self._effective_gate

    def advance_phase(self, new_phase: SessionPhase) -> None:
        """Transition to *new_phase* and recompute the effective gate."""
        if new_phase == self._phase:
            return
        old = self._phase
        self._phase = new_phase
        self._last_phase_change_ns = timebase.now_ns()
        self._recompute_gate()
        logger.info(
            "session_phase_changed",
            old_phase=old.value,
            new_phase=new_phase.value,
            effective_gate=self._effective_gate.name,
        )
        if self._on_phase_change is not None:
            try:
                self._on_phase_change(new_phase)
            except Exception:
                pass

    # ---- Override gate (external callers) ----

    def set_override_gate(self, gate: TrackGate) -> None:
        """Set an external override gate (e.g. from autonomy monitor)."""
        self._override_gate = gate
        self._recompute_gate()
        logger.info("override_gate_set", gate=gate.name, effective_gate=self._effective_gate.name)

    def clear_override_gate(self) -> None:
        """Remove the external override, reverting to phase-only gate."""
        self._override_gate = TrackGate.OPEN
        self._recompute_gate()
        logger.info("override_gate_cleared", effective_gate=self._effective_gate.name)

    # ---- Query ----

    def allows_new_orders(self) -> bool:
        return self._effective_gate == TrackGate.OPEN

    def allows_close_orders(self) -> bool:
        return self._effective_gate <= TrackGate.CLOSE_ONLY

    def is_locked(self) -> bool:
        return self._effective_gate == TrackGate.LOCKED

    # ---- Internal ----

    def _recompute_gate(self) -> None:
        phase_gate = _PHASE_TO_GATE.get(self._phase, TrackGate.LOCKED)
        self._effective_gate = max(phase_gate, self._override_gate)

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self._phase.value,
            "override_gate": self._override_gate.name,
            "effective_gate": self._effective_gate.name,
            "last_phase_change_ns": self._last_phase_change_ns,
        }
