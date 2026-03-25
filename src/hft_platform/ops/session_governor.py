"""SessionGovernor: per-product session phase state machine with TrackGate.

Manages multi-track trading sessions (stock, futures_day, futures_night) with
wall-clock phase transitions. Provides a lightweight TrackGate for StrategyRunner
to filter intents per-symbol based on current session phase.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog
import yaml

logger = structlog.get_logger("ops.session_governor")

# ---------------------------------------------------------------------------
# Default config path relative to project root
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "base" / "session_governor.yaml"

# Phase ordering for scheduled transitions
_PHASE_ORDER: list[str] = ["pre_open", "open", "close_only", "force_flat", "closed"]


# ---------------------------------------------------------------------------
# SessionPhase enum
# ---------------------------------------------------------------------------
class SessionPhase(IntEnum):
    """Trading session phase for a track."""

    INIT = 0
    PRE_OPEN = 1
    OPEN = 2
    CLOSE_ONLY = 3
    FORCE_FLAT = 4
    CLOSED = 5


_PHASE_NAME_MAP: dict[str, SessionPhase] = {
    "pre_open": SessionPhase.PRE_OPEN,
    "open": SessionPhase.OPEN,
    "close_only": SessionPhase.CLOSE_ONLY,
    "force_flat": SessionPhase.FORCE_FLAT,
    "closed": SessionPhase.CLOSED,
}


# ---------------------------------------------------------------------------
# TrackConfig
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TrackConfig:
    """Configuration for a single session track."""

    name: str
    symbols: list[str] = field(default_factory=list)
    schedule: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TrackGate
# ---------------------------------------------------------------------------
class TrackGate:
    """Lightweight lookup: symbol -> track_name -> current SessionPhase.

    Designed to be read from the strategy hot path with minimal overhead.
    """

    __slots__ = ("_symbol_to_track", "_track_phases")

    def __init__(self) -> None:
        self._symbol_to_track: dict[str, str] = {}
        self._track_phases: dict[str, SessionPhase] = {}

    def register_symbol(self, symbol: str, track_name: str) -> None:
        """Register a symbol to a track."""
        self._symbol_to_track[symbol] = track_name

    def set_track_phase(self, track_name: str, phase: SessionPhase) -> None:
        """Update the current phase for a track."""
        self._track_phases[track_name] = phase

    def get_phase(self, symbol: str) -> SessionPhase:
        """Return current phase for *symbol*. Unknown symbols default to OPEN."""
        track = self._symbol_to_track.get(symbol)
        if track is None:
            return SessionPhase.OPEN
        return self._track_phases.get(track, SessionPhase.OPEN)

    @property
    def track_phases(self) -> dict[str, SessionPhase]:
        """Read-only snapshot of track -> phase mapping."""
        return dict(self._track_phases)

    @property
    def symbol_to_track(self) -> dict[str, str]:
        """Read-only snapshot of symbol -> track mapping."""
        return dict(self._symbol_to_track)


# ---------------------------------------------------------------------------
# SessionGovernor
# ---------------------------------------------------------------------------
class SessionGovernor:
    """Manages per-product session phases using wall-clock scheduling.

    The governor does NOT start/stop StrategyRunner. Instead it maintains a
    TrackGate that the runner reads per-event to filter intents.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        evidence_writer: Any | None = None,
        notification_dispatcher: Any | None = None,
        position_flattener: Any | None = None,
        market_calendar: Any | None = None,
    ) -> None:
        self._tracks: dict[str, TrackConfig] = {}
        self._track_gate = TrackGate()
        self._tz = ZoneInfo("Asia/Taipei")
        self._evidence_writer = evidence_writer
        self._notification_dispatcher = notification_dispatcher
        self._position_flattener = position_flattener
        self._market_calendar = market_calendar
        self._phase_callbacks: list[Callable[[str, SessionPhase, SessionPhase], None]] = []
        self._running = False

        cfg = config_path or _DEFAULT_CONFIG_PATH
        self._load_config(Path(cfg) if isinstance(cfg, str) else cfg)

    def _load_config(self, path: Path) -> None:
        """Load track configuration from YAML."""
        if not path.exists():
            logger.warning("session_governor_config_not_found", path=str(path))
            return
        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            for track_name, track_cfg in raw.get("tracks", {}).items():
                cfg = TrackConfig(
                    name=track_name,
                    symbols=list(track_cfg.get("symbols", [])),
                    schedule=list(track_cfg.get("schedule", [])),
                )
                self._tracks[track_name] = cfg
                for symbol in cfg.symbols:
                    self._track_gate.register_symbol(symbol, track_name)
                # Initialize all tracks to INIT phase
                self._track_gate.set_track_phase(track_name, SessionPhase.INIT)
            logger.info("session_governor_config_loaded", tracks=list(self._tracks.keys()))
        except Exception as exc:  # noqa: BLE001
            logger.error("session_governor_config_load_failed", error=str(exc))

    @property
    def track_gate(self) -> TrackGate:
        """Expose the TrackGate for injection into StrategyRunner."""
        return self._track_gate

    def register_phase_callback(self, callback: Callable[[str, SessionPhase, SessionPhase], None]) -> None:
        """Register a callback invoked on phase transitions: (track, old, new)."""
        self._phase_callbacks.append(callback)

    def transition_track(self, track_name: str, new_phase: SessionPhase) -> None:
        """Manually transition a track to a new phase."""
        old_phase = self._track_gate._track_phases.get(track_name, SessionPhase.INIT)
        if old_phase == new_phase:
            return
        self._track_gate.set_track_phase(track_name, new_phase)
        logger.info(
            "session_phase_transition",
            track=track_name,
            old=old_phase.name,
            new=new_phase.name,
        )
        for cb in self._phase_callbacks:
            try:
                cb(track_name, old_phase, new_phase)
            except Exception as exc:  # noqa: BLE001
                logger.error("session_phase_callback_error", error=str(exc))

    def get_phase(self, symbol: str) -> SessionPhase:
        """Return current phase for a symbol."""
        return self._track_gate.get_phase(symbol)

    async def run(self) -> None:
        """Run the governor's scheduling loop."""
        self._running = True
        logger.info("session_governor_started")
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("session_governor_stopped")

    def stop(self) -> None:
        """Stop the governor."""
        self._running = False
