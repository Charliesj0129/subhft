"""SessionGovernor: per-product session phase state machine with TrackGate.

Manages multi-track trading sessions (stock, futures_day, futures_night) with
wall-clock phase transitions. Provides a lightweight TrackGate for StrategyRunner
to filter intents per-symbol based on current session phase.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time
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

    __slots__ = ("_symbol_to_track", "_track_phases", "_warned_unknown", "_default_open")

    def __init__(self) -> None:
        self._symbol_to_track: dict[str, str] = {}
        self._track_phases: dict[str, SessionPhase] = {}
        self._warned_unknown: set[str] = set()
        self._default_open: bool = os.getenv("HFT_TRACK_GATE_DEFAULT_OPEN", "0") in ("1", "true", "yes")

    def register_symbol(self, symbol: str, track_name: str) -> None:
        """Register a symbol to a track."""
        self._symbol_to_track[symbol] = track_name

    def set_track_phase(self, track_name: str, phase: SessionPhase) -> None:
        """Update the current phase for a track."""
        self._track_phases[track_name] = phase

    def get_phase(self, symbol: str) -> SessionPhase:
        """Return current phase for *symbol*. Unknown symbols default to CLOSED."""
        track = self._symbol_to_track.get(symbol)
        if track is None:
            if self._default_open:
                return SessionPhase.OPEN
            if symbol not in self._warned_unknown:
                self._warned_unknown.add(symbol)
                logger.warning("track_gate_unknown_symbol_blocked", symbol=symbol, default_phase="CLOSED")
            return SessionPhase.CLOSED
        return self._track_phases.get(track, SessionPhase.CLOSED)

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
        self._task: asyncio.Task[None] | None = None
        self._poll_interval_s = float(os.getenv("HFT_SESSION_GOVERNOR_POLL_INTERVAL_S", "1.0"))

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

    @staticmethod
    def _parse_hhmm(value: str) -> int:
        hour_str, minute_str = value.split(":", 1)
        return (int(hour_str) * 60) + int(minute_str)

    @staticmethod
    def _minutes_since_midnight(dt_local: datetime) -> int:
        return (dt_local.hour * 60) + dt_local.minute

    def _schedule_points(self, track_name: str, current_dt: datetime) -> list[tuple[datetime, SessionPhase]]:
        cfg = self._tracks.get(track_name)
        if cfg is None or not cfg.schedule:
            return []

        schedule_minutes = [self._parse_hhmm(str(item["time"])) for item in cfg.schedule]
        overnight = any(cur < prev for prev, cur in zip(schedule_minutes, schedule_minutes[1:], strict=False))

        anchor_date = current_dt.date()
        if overnight and schedule_minutes:
            current_minutes = self._minutes_since_midnight(current_dt)
            if current_minutes < schedule_minutes[0]:
                anchor_date = current_dt.date() - timedelta(days=1)

        points: list[tuple[datetime, SessionPhase]] = []
        point_date = anchor_date
        prev_minutes: int | None = None
        for item, minutes in zip(cfg.schedule, schedule_minutes, strict=False):
            if prev_minutes is not None and minutes < prev_minutes:
                point_date += timedelta(days=1)
            phase_key = str(item["phase"]).strip().lower()
            if phase_key not in _PHASE_NAME_MAP:
                valid = ", ".join(sorted(_PHASE_NAME_MAP))
                msg = f"Invalid session phase '{item['phase']}' in track config. Valid phases: {valid}"
                raise ValueError(msg)
            phase = _PHASE_NAME_MAP[phase_key]
            points.append(
                (
                    datetime.combine(point_date, dt_time(hour=minutes // 60, minute=minutes % 60), tzinfo=self._tz),
                    phase,
                )
            )
            prev_minutes = minutes
        return points

    def _phase_for_dt(self, track_name: str, dt_local: datetime) -> SessionPhase:
        if dt_local.tzinfo is None:
            dt_local = dt_local.replace(tzinfo=self._tz)
        points = self._schedule_points(track_name, dt_local)
        if not points:
            return SessionPhase.CLOSED

        phase = SessionPhase.CLOSED
        for point_dt, point_phase in points:
            if dt_local >= point_dt:
                phase = point_phase
            else:
                break
        return phase

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
        if new_phase == SessionPhase.FORCE_FLAT and self._position_flattener is not None:
            track_cfg = self._tracks.get(track_name)
            if track_cfg is not None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    logger.warning("session_force_flat_without_running_loop", track=track_name)
                else:
                    task = loop.create_task(self._position_flattener.flatten_track(track_name, list(track_cfg.symbols)))
                    task._flatten_retry_count = 0  # type: ignore[attr-defined]
                    task._flatten_track_name = track_name  # type: ignore[attr-defined]
                    task.add_done_callback(self._on_flatten_task_done)
        for cb in self._phase_callbacks:
            try:
                cb(track_name, old_phase, new_phase)
            except Exception as exc:  # noqa: BLE001
                logger.error("session_phase_callback_error", error=str(exc))

    _MAX_FLATTEN_RETRIES: int = 2

    def _on_flatten_task_done(self, task: asyncio.Task) -> None:  # type: ignore[type-arg]
        """Log errors from fire-and-forget flatten tasks; retry up to _MAX_FLATTEN_RETRIES times."""
        if task.cancelled():
            logger.warning("session_flatten_task_cancelled")
            return
        exc = task.exception()
        if exc is not None:
            retry_count = getattr(task, "_flatten_retry_count", 0)
            track_name = getattr(task, "_flatten_track_name", "unknown")
            logger.critical(
                "session_flatten_task_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                retry=retry_count,
                track=track_name,
            )
            # Send notification if dispatcher available
            if self._notification_dispatcher is not None:
                try:
                    asyncio.ensure_future(
                        self._notification_dispatcher.notify_flatten_result(
                            scope=track_name,
                            fully_closed=0,
                            partially_closed=0,
                            failed=1,
                            failed_symbols=[f"flatten_exception: {type(exc).__name__}"],
                        )
                    )
                except Exception as _notify_exc:  # noqa: BLE001
                    logger.warning("session_flatten_notify_failed", error=str(_notify_exc))

            # Retry if under limit
            if retry_count < self._MAX_FLATTEN_RETRIES and self._position_flattener is not None:
                track_cfg = self._tracks.get(track_name)
                if track_cfg is not None:
                    logger.warning(
                        "session_flatten_task_retry",
                        track=track_name,
                        retry=retry_count + 1,
                    )
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        return
                    new_task = loop.create_task(
                        self._position_flattener.flatten_track(track_name, list(track_cfg.symbols))
                    )
                    new_task._flatten_retry_count = retry_count + 1  # type: ignore[attr-defined]
                    new_task._flatten_track_name = track_name  # type: ignore[attr-defined]
                    new_task.add_done_callback(self._on_flatten_task_done)

    def get_phase(self, symbol: str) -> SessionPhase:
        """Return current phase for a symbol."""
        return self._track_gate.get_phase(symbol)

    async def start(self) -> None:
        """Start the background scheduling loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run())
        await asyncio.sleep(0)

    async def run(self) -> None:
        """Run the governor's scheduling loop."""
        self._running = True
        logger.info("session_governor_started")
        try:
            while self._running:
                now = datetime.now(self._tz)
                for track_name in self._tracks:
                    self.transition_track(track_name, self._phase_for_dt(track_name, now))
                await asyncio.sleep(self._poll_interval_s)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("session_governor_stopped")

    async def stop(self) -> None:
        """Stop the governor."""
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
