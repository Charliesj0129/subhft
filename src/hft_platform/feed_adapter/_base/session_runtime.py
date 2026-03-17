"""Abstract base for broker session lifecycle management.

Captures the shared login-retry-reconnect-cooldown pattern observed in both
Shioaji ``SessionRuntime`` and Fubon ``FubonSessionRuntime``.  Concrete
subclasses implement the broker-specific ``_do_login``, ``_do_logout``, and
``_do_reconnect`` hooks; the base class owns the backoff FSM and cooldown
guard.

This is an ADDITIVE extraction — existing Shioaji/Fubon implementations
are not modified.  The base class documents shared patterns and is available
for future refactors that want to reduce duplication.
"""

from __future__ import annotations

import abc
import time
from typing import Any

import structlog

logger = structlog.get_logger("feed_adapter._base.session_runtime")


class BaseBrokerSessionRuntime(abc.ABC):
    """Abstract session lifecycle: login, logout, reconnect with backoff.

    Shared patterns extracted from Shioaji and Fubon session runtimes:

    * **Credential resolution** from explicit args or environment variables
    * **Login with retry** using exponential backoff
    * **Reconnect** with cooldown guard to prevent reconnect storms
    * **Login error tracking** for diagnostics

    Subclasses must implement:
      - ``_do_login(**credentials) -> bool``
      - ``_do_logout() -> None``
      - ``_do_reconnect(reason, force) -> bool``
      - ``_resolve_credentials() -> dict[str, Any]``
    """

    __slots__ = (
        "_logged_in",
        "_last_login_error",
        "_last_reconnect_ns",
        "_reconnect_cooldown_s",
        "_login_retry_max",
        "_backoff_base_s",
    )

    def __init__(
        self,
        *,
        reconnect_cooldown_s: float = 5.0,
        login_retry_max: int = 3,
        backoff_base_s: float = 1.0,
    ) -> None:
        self._logged_in: bool = False
        self._last_login_error: str | None = None
        self._last_reconnect_ns: int = 0
        self._reconnect_cooldown_s: float = reconnect_cooldown_s
        self._login_retry_max: int = login_retry_max
        self._backoff_base_s: float = backoff_base_s

    # ------------------------------------------------------------------ #
    # Abstract hooks — broker-specific
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def _do_login(self, **credentials: Any) -> bool:
        """Perform the broker-specific login call.

        Returns ``True`` on success.  On failure, the implementation should
        set ``self._last_login_error`` with a descriptive message before
        returning ``False``.
        """

    @abc.abstractmethod
    def _do_logout(self) -> None:
        """Perform the broker-specific logout/disconnect call.

        Must not raise; log warnings internally on error.
        """

    @abc.abstractmethod
    def _do_reconnect(self, reason: str, force: bool) -> bool:
        """Perform broker-specific reconnect logic.

        Called after the cooldown guard passes.  Implementations typically
        call ``_do_logout()`` then ``login_with_retry()``.

        Returns ``True`` on successful reconnect.
        """

    @abc.abstractmethod
    def _resolve_credentials(self) -> dict[str, Any]:
        """Resolve login credentials from args/env.

        Returns a dict of keyword arguments to pass to ``_do_login()``.
        Implementations should fall back to environment variables when
        explicit values are not configured.
        """

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def is_logged_in(self) -> bool:
        """Return ``True`` if the session is currently authenticated."""
        return self._logged_in

    @property
    def last_login_error(self) -> str | None:
        """Return the last login error message, or ``None``."""
        return self._last_login_error

    # ------------------------------------------------------------------ #
    # Login with retry (shared backoff FSM)
    # ------------------------------------------------------------------ #

    def login_with_retry(
        self,
        max_retries: int | None = None,
        backoff_base_s: float | None = None,
        **extra_credentials: Any,
    ) -> bool:
        """Login with exponential backoff retry.

        Tries up to ``max_retries`` times (default: ``self._login_retry_max``)
        with delays of ``backoff_base_s * 2**attempt`` seconds between
        attempts.

        Extra keyword arguments are merged with ``_resolve_credentials()``
        results and forwarded to ``_do_login()``.

        Returns ``True`` on first successful login.
        """
        retries = max_retries if max_retries is not None else self._login_retry_max
        base_s = backoff_base_s if backoff_base_s is not None else self._backoff_base_s

        credentials = self._resolve_credentials()
        credentials.update(extra_credentials)

        for attempt in range(retries):
            logger.info(
                "broker_login_attempt",
                attempt=attempt + 1,
                max=retries,
            )
            if self._do_login(**credentials):
                self._logged_in = True
                self._last_login_error = None
                return True

            if attempt < retries - 1:
                sleep_s = base_s * (2**attempt)
                logger.warning(
                    "broker_login_retry_backoff",
                    attempt=attempt + 1,
                    sleep_s=sleep_s,
                    error=self._last_login_error,
                )
                time.sleep(sleep_s)

        logger.error(
            "broker_login_retries_exhausted",
            max_retries=retries,
            error=self._last_login_error,
        )
        return False

    # ------------------------------------------------------------------ #
    # Reconnect with cooldown guard
    # ------------------------------------------------------------------ #

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Reconnect the broker session, respecting a cooldown guard.

        The cooldown prevents reconnect storms.  Set *force=True* to
        bypass it.

        Returns ``True`` on successful reconnect.
        """
        now_ns = time.perf_counter_ns()
        elapsed_s = (now_ns - self._last_reconnect_ns) / 1_000_000_000

        if not force and self._last_reconnect_ns > 0 and elapsed_s < self._reconnect_cooldown_s:
            logger.info(
                "broker_reconnect_cooldown",
                elapsed_s=round(elapsed_s, 2),
                cooldown_s=self._reconnect_cooldown_s,
                reason=reason,
            )
            return False

        self._last_reconnect_ns = now_ns
        logger.info("broker_reconnect_start", reason=reason, force=force)
        return self._do_reconnect(reason=reason, force=force)

    # ------------------------------------------------------------------ #
    # Logout
    # ------------------------------------------------------------------ #

    def logout(self) -> None:
        """Logout from the broker session."""
        self._do_logout()
        self._logged_in = False

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        """Return a dict describing the current session state."""
        return {
            "logged_in": self._logged_in,
            "last_login_error": self._last_login_error,
            "last_reconnect_ns": self._last_reconnect_ns,
            "reconnect_cooldown_s": self._reconnect_cooldown_s,
        }
