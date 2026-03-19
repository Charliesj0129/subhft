"""Config hot-reload watcher for strategy_limits.yaml.

Polls the config file via ``os.stat()`` mtime and invokes registered callbacks
when the file changes. Also supports SIGHUP as an alternative reload trigger.

Disabled by default (``HFT_HOT_RELOAD_ENABLED=0``).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any, Callable, Dict, List

import yaml
from structlog import get_logger

logger = get_logger("config.hot_reload")

# Default poll interval in seconds
_DEFAULT_POLL_INTERVAL_S: float = 5.0

# Maximum number of registered callbacks (bounded per CE2-12 governance)
_MAX_CALLBACKS: int = 64


class ConfigWatcher:
    """Async background watcher that detects changes to a YAML config file.

    Parameters
    ----------
    config_path:
        Absolute or relative path to the YAML file to watch.
    poll_interval_s:
        Seconds between ``os.stat()`` polls (default 5).
    """

    __slots__ = (
        "_config_path",
        "_poll_interval_s",
        "_callbacks",
        "_last_mtime",
        "_current_config",
        "_task",
        "_sighup_registered",
    )

    def __init__(
        self,
        config_path: str,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._config_path: str = config_path
        self._poll_interval_s: float = max(1.0, poll_interval_s)
        self._callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._last_mtime: float = 0.0
        self._current_config: Dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None
        self._sighup_registered: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register a callback to be invoked with the new config dict on reload.

        Raises ``RuntimeError`` if the callback limit is exceeded.
        """
        if len(self._callbacks) >= _MAX_CALLBACKS:
            raise RuntimeError(f"ConfigWatcher: callback limit exceeded ({_MAX_CALLBACKS})")
        self._callbacks.append(callback)

    @property
    def current_config(self) -> Dict[str, Any]:
        return self._current_config

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the background polling task.

        Must be called from within a running event loop (or provide *loop*).
        """
        if self._task is not None:
            return  # already running

        # Initial load
        self._load_and_store()

        self._task = asyncio.ensure_future(self._poll_loop())

        # Register SIGHUP handler (Unix only, non-fatal if unavailable)
        if not self._sighup_registered and sys.platform != "win32":
            try:
                ev_loop = loop or asyncio.get_running_loop()
                ev_loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
                self._sighup_registered = True
                logger.info("ConfigWatcher: SIGHUP handler registered")
            except (ValueError, OSError, RuntimeError) as exc:
                logger.debug("ConfigWatcher: SIGHUP handler not available", error=str(exc))

    async def stop(self) -> None:
        """Cancel the background task."""
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_sighup(self) -> None:
        """Handle SIGHUP by scheduling an immediate reload check."""
        logger.info("ConfigWatcher: SIGHUP received, triggering reload")
        asyncio.ensure_future(self._check_and_reload())

    async def _poll_loop(self) -> None:
        """Background loop that checks file mtime periodically."""
        while True:
            await asyncio.sleep(self._poll_interval_s)
            await self._check_and_reload()

    async def _check_and_reload(self) -> None:
        """Compare mtime and reload if changed."""
        try:
            stat = os.stat(self._config_path)
            mtime = stat.st_mtime
        except FileNotFoundError:
            logger.warning("ConfigWatcher: config file not found", path=self._config_path)
            return
        except OSError as exc:
            logger.warning("ConfigWatcher: stat failed", path=self._config_path, error=str(exc))
            return

        if mtime == self._last_mtime:
            return

        logger.info(
            "ConfigWatcher: config file changed, reloading",
            path=self._config_path,
            old_mtime=self._last_mtime,
            new_mtime=mtime,
        )
        self._load_and_notify(mtime)

    def _load_and_store(self) -> None:
        """Initial load without notifying callbacks."""
        try:
            stat = os.stat(self._config_path)
            mtime = stat.st_mtime
        except OSError:
            mtime = 0.0

        new_config = self._safe_load_yaml()
        if new_config is not None:
            self._current_config = new_config
            self._last_mtime = mtime

    def _load_and_notify(self, mtime: float) -> None:
        """Load YAML, validate, and notify callbacks. Keep old config on failure."""
        new_config = self._safe_load_yaml()
        if new_config is None:
            # Invalid YAML — keep old config, do NOT crash
            logger.error(
                "ConfigWatcher: invalid YAML, keeping old config",
                path=self._config_path,
            )
            # Still update mtime to avoid spamming reload attempts
            self._last_mtime = mtime
            return

        self._current_config = new_config
        self._last_mtime = mtime

        for cb in self._callbacks:
            try:
                cb(new_config)
            except Exception as exc:
                logger.error(
                    "ConfigWatcher: callback error",
                    callback=getattr(cb, "__qualname__", str(cb)),
                    error=str(exc),
                )

    def _safe_load_yaml(self) -> Dict[str, Any] | None:
        """Load and validate the YAML file. Returns None on any error."""
        try:
            with open(self._config_path, "r") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                logger.error(
                    "ConfigWatcher: YAML root is not a dict",
                    path=self._config_path,
                    type=type(data).__name__,
                )
                return None
            return data
        except yaml.YAMLError as exc:
            logger.error(
                "ConfigWatcher: YAML parse error",
                path=self._config_path,
                error=str(exc),
            )
            return None
        except OSError as exc:
            logger.error(
                "ConfigWatcher: file read error",
                path=self._config_path,
                error=str(exc),
            )
            return None
