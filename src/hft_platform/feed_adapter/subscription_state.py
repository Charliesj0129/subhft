"""Persistent subscription state for cross-session recovery.

This module provides functionality to persist subscription state to disk,
enabling recovery of subscribed symbols after crashes or restarts.
"""

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("subscription_state")

DEFAULT_STATE_PATH = ".state/subscriptions.json"


@dataclass
class SymbolState:
    """State for a single subscribed symbol."""

    code: str
    exchange: str
    product_type: str = ""
    subscribed_at_ns: int = 0
    last_tick_ts_ns: int = 0
    tick_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SymbolState":
        return cls(
            code=data.get("code", ""),
            exchange=data.get("exchange", ""),
            product_type=data.get("product_type", ""),
            subscribed_at_ns=int(data.get("subscribed_at_ns", 0)),
            last_tick_ts_ns=int(data.get("last_tick_ts_ns", 0)),
            tick_count=int(data.get("tick_count", 0)),
        )


@dataclass
class SubscriptionStateData:
    """Complete subscription state for persistence."""

    version: int = 1
    session_id: str = ""
    last_save_ts_ns: int = 0
    symbols: dict[str, SymbolState] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "last_save_ts_ns": self.last_save_ts_ns,
            "symbols": {k: v.to_dict() for k, v in self.symbols.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubscriptionStateData":
        symbols = {}
        for k, v in data.get("symbols", {}).items():
            symbols[k] = SymbolState.from_dict(v)
        return cls(
            version=int(data.get("version", 1)),
            session_id=str(data.get("session_id", "")),
            last_save_ts_ns=int(data.get("last_save_ts_ns", 0)),
            symbols=symbols,
        )


class SubscriptionStateManager:
    """Manages persistent subscription state with atomic writes."""

    _instance: "SubscriptionStateManager | None" = None

    def __init__(self, state_path: str | None = None):
        self._state_path: str = state_path or os.getenv(
            "HFT_SUBSCRIPTION_STATE_PATH"
        ) or DEFAULT_STATE_PATH
        self._state = SubscriptionStateData()
        self._lock = threading.Lock()
        self._dirty = False
        self._session_id = f"{timebase.now_ns()}"
        self._state.session_id = self._session_id
        self._auto_save_interval_s = float(
            os.getenv("HFT_SUBSCRIPTION_AUTOSAVE_S", "5.0")
        )
        self._auto_save_thread: threading.Thread | None = None
        self._running = False

    @classmethod
    def get(cls, state_path: str | None = None) -> "SubscriptionStateManager":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls(state_path)
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Reset singleton for testing."""
        if cls._instance is not None:
            cls._instance.stop()
        cls._instance = None

    def load(self) -> bool:
        """Load subscription state from disk. Returns True if state was loaded."""
        try:
            path = Path(self._state_path)
            if not path.exists():
                logger.debug("No existing subscription state file", path=self._state_path)
                return False

            with open(path, "r") as f:
                data = json.load(f)

            with self._lock:
                self._state = SubscriptionStateData.from_dict(data)
                # Generate new session ID for this run
                self._session_id = f"{timebase.now_ns()}"
                self._state.session_id = self._session_id
                self._dirty = False

            logger.info(
                "Loaded subscription state",
                path=self._state_path,
                symbol_count=len(self._state.symbols),
                previous_session=data.get("session_id", "unknown"),
            )
            return True

        except json.JSONDecodeError as e:
            logger.error(
                "Failed to parse subscription state file",
                path=self._state_path,
                error=str(e),
            )
            return False
        except Exception as e:
            logger.error(
                "Failed to load subscription state",
                path=self._state_path,
                error=str(e),
            )
            return False

    def save(self, force: bool = False) -> bool:
        """Save subscription state to disk atomically.

        Uses temp file + rename pattern for atomic writes.
        """
        with self._lock:
            if not force and not self._dirty:
                return True

            self._state.last_save_ts_ns = timebase.now_ns()
            data = self._state.to_dict()

        try:
            path = Path(self._state_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write: write to temp file, then rename
            tmp_path = path.with_suffix(".json.tmp")
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            tmp_path.rename(path)

            with self._lock:
                self._dirty = False

            logger.debug(
                "Saved subscription state",
                path=self._state_path,
                symbol_count=len(self._state.symbols),
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to save subscription state",
                path=self._state_path,
                error=str(e),
            )
            return False

    def add_symbol(
        self,
        code: str,
        exchange: str,
        product_type: str = "",
    ) -> None:
        """Add or update a subscribed symbol."""
        with self._lock:
            key = f"{exchange}:{code}"
            if key in self._state.symbols:
                # Update existing - preserve tick stats
                existing = self._state.symbols[key]
                existing.product_type = product_type or existing.product_type
            else:
                self._state.symbols[key] = SymbolState(
                    code=code,
                    exchange=exchange,
                    product_type=product_type,
                    subscribed_at_ns=timebase.now_ns(),
                )
            self._dirty = True

    def remove_symbol(self, code: str, exchange: str) -> None:
        """Remove a symbol from subscription state."""
        with self._lock:
            key = f"{exchange}:{code}"
            if key in self._state.symbols:
                del self._state.symbols[key]
                self._dirty = True

    def record_tick(self, code: str, exchange: str, tick_ts_ns: int | None = None) -> None:
        """Record a tick for a symbol."""
        with self._lock:
            key = f"{exchange}:{code}"
            if key in self._state.symbols:
                sym = self._state.symbols[key]
                sym.last_tick_ts_ns = tick_ts_ns or timebase.now_ns()
                sym.tick_count += 1
                self._dirty = True

    def get_symbols(self) -> list[dict[str, Any]]:
        """Get all subscribed symbols in format suitable for subscribe_basket."""
        with self._lock:
            return [
                {
                    "code": sym.code,
                    "exchange": sym.exchange,
                    "product_type": sym.product_type,
                }
                for sym in self._state.symbols.values()
            ]

    def get_symbol_state(self, code: str, exchange: str) -> SymbolState | None:
        """Get state for a specific symbol."""
        with self._lock:
            key = f"{exchange}:{code}"
            return self._state.symbols.get(key)

    def clear(self) -> None:
        """Clear all subscription state."""
        with self._lock:
            self._state.symbols.clear()
            self._dirty = True

    def get_stale_symbols(self, max_gap_s: float = 60.0) -> list[str]:
        """Get symbols that haven't received data within the gap threshold."""
        now_ns = timebase.now_ns()
        max_gap_ns = int(max_gap_s * 1e9)
        stale = []
        with self._lock:
            for key, sym in self._state.symbols.items():
                if sym.last_tick_ts_ns > 0:
                    gap = now_ns - sym.last_tick_ts_ns
                    if gap > max_gap_ns:
                        stale.append(key)
        return stale

    def start_auto_save(self) -> None:
        """Start background auto-save thread."""
        if self._running:
            return
        self._running = True

        def _auto_save_loop() -> None:
            import time
            while self._running:
                time.sleep(self._auto_save_interval_s)
                if self._dirty:
                    self.save()

        self._auto_save_thread = threading.Thread(
            target=_auto_save_loop,
            name="subscription-state-autosave",
            daemon=True,
        )
        self._auto_save_thread.start()
        logger.info(
            "Started subscription state auto-save",
            interval_s=self._auto_save_interval_s,
        )

    def stop(self) -> None:
        """Stop auto-save and save final state."""
        self._running = False
        if self._auto_save_thread is not None:
            self._auto_save_thread.join(timeout=2.0)
            self._auto_save_thread = None
        self.save(force=True)

    @property
    def symbol_count(self) -> int:
        """Get count of subscribed symbols."""
        with self._lock:
            return len(self._state.symbols)

    @property
    def session_id(self) -> str:
        """Get current session ID."""
        return self._session_id
