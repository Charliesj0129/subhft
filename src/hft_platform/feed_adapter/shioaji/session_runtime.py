from __future__ import annotations

import datetime as dt
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from structlog import get_logger

from hft_platform.core import timebase

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.session_runtime")


@dataclass(frozen=True)
class SessionStateSnapshot:
    logged_in: bool
    reconnect_backoff_s: float
    last_login_error: str | None
    last_reconnect_error: str | None


@runtime_checkable
class SessionPolicy(Protocol):
    """Interface for session lifecycle decisions.

    The quote watchdog and quote event handlers must interact with the
    session lifecycle only through this protocol — never by directly
    importing or calling ShioajiClient internals. This breaks the
    circular dependency between quote recovery logic and session state.

    Implementors:
      - SessionRuntime: delegates to the legacy ShioajiClient
      - (Future) StandaloneSessionRuntime: owns login/reconnect/backoff FSM
    """

    def request_reconnect(self, reason: str, force: bool = False) -> bool:
        """Request a session reconnect.

        Returns True if reconnect was initiated or succeeded;
        False if gated out (cooldown, non-trading hours, lock busy).
        Must never raise.
        """
        ...

    def is_logged_in(self) -> bool:
        """Return True if the broker session is currently authenticated."""
        ...


class SessionRuntime:
    """Manages session lifecycle: login, refresh, reconnect.

    Phase-2 decoupling: owns login/session_refresh/do_session_refresh logic.
    ShioajiClient.login() / _start_session_refresh_thread() / _do_session_refresh()
    are now thin delegation stubs that call into this class.
    Phase-3 target: own the full reconnect/backoff FSM.

    Implements ``SessionPolicy`` so quote-side code can talk to session-side
    code exclusively through the protocol interface, enabling independent
    testing and future FSM extraction.
    """

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    # ------------------------------------------------------------------ #
    # SessionPolicy implementation
    # ------------------------------------------------------------------ #

    def request_reconnect(self, reason: str, force: bool = False) -> bool:
        """Delegate reconnect request to the underlying client.

        The client's reconnect() respects backoff, lock, and cooldown guards.
        Returns False if gated out rather than raising.
        """
        try:
            return bool(self._client.reconnect(reason=reason, force=force))
        except Exception:
            return False

    def is_logged_in(self) -> bool:
        return bool(getattr(self._client, "logged_in", False))

    # ------------------------------------------------------------------ #
    # Login lifecycle (Phase-2: owned here, not in ShioajiClient)
    # ------------------------------------------------------------------ #

    def login(self, *args, **kwargs) -> bool:
        """Public entrypoint — calls login_with_retry."""
        return self.login_with_retry(*args, **kwargs)

    def login_with_retry(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        person_id: str | None = None,
        ca_passwd: str | None = None,
        contracts_cb: Any = None,
    ) -> bool:
        """Full login sequence with retry, CA activation, and contract fetch fallback.

        Extracted from ShioajiClient.login() — all state reads/writes go through
        self._client to maintain a single source of truth.
        """
        c = self._client
        logger.info("Logging in to Shioaji...")
        c.ca_active = False
        c.logged_in = False
        c._last_login_error = None

        key = api_key or os.getenv("SHIOAJI_API_KEY")
        secret = secret_key or os.getenv("SHIOAJI_SECRET_KEY")
        pid = person_id or os.getenv("SHIOAJI_PERSON_ID")
        ca_pwd = ca_passwd or os.getenv("SHIOAJI_CA_PASSWORD") or os.getenv("CA_PASSWORD")

        if key and secret:
            if c.api is None:
                logger.warning("Shioaji SDK unavailable; cannot login with credentials.")
                return False
            c._ensure_session_lock()
            logger.info("Using API Key/Secret for login")
            fallback_enabled = os.getenv("HFT_LOGIN_FETCH_CONTRACT_FALLBACK", "1").lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
            attempts_total = max(1, c._login_retry_max + 1)

            def _do_login(fetch_contract: bool) -> None:
                c.api.login(
                    api_key=key,
                    secret_key=secret,
                    contracts_timeout=c.contracts_timeout,
                    contracts_cb=contracts_cb,
                    fetch_contract=fetch_contract,
                    subscribe_trade=c.subscribe_trade,
                )

            for attempt in range(1, attempts_total + 1):
                login_fetch_contract = c.fetch_contract
                start_ns = time.perf_counter_ns()
                ok, _, err, timed_out = c._safe_call_with_timeout(
                    "login",
                    lambda: _do_login(login_fetch_contract),
                    c._login_timeout_s,
                )
                c._record_api_latency("login", start_ns, ok=ok)
                if not ok:
                    c._last_login_error = str(err) if err is not None else "unknown"
                    if login_fetch_contract and fallback_enabled:
                        logger.warning(
                            "Login failed with contract fetch; retrying without contracts",
                            attempt=attempt,
                            timeout=timed_out,
                            error=c._last_login_error,
                        )
                        start_ns = time.perf_counter_ns()
                        ok_fb, _, err_fb, timed_out_fb = c._safe_call_with_timeout(
                            "login_fallback",
                            lambda: _do_login(False),
                            c._login_timeout_s,
                        )
                        c._record_api_latency("login", start_ns, ok=ok_fb)
                        if ok_fb:
                            login_fetch_contract = False
                            c.fetch_contract = False
                            ok = True
                        else:
                            c._last_login_error = str(err_fb) if err_fb is not None else "unknown"
                            logger.error(
                                "Login fallback (no-contract) failed",
                                attempt=attempt,
                                timeout=timed_out_fb,
                                error=c._last_login_error,
                            )
                    else:
                        logger.error(
                            "Login attempt failed",
                            attempt=attempt,
                            timeout=timed_out,
                            error=c._last_login_error,
                        )

                if ok:
                    logger.info("Login successful (API Key)", attempt=attempt)
                    if not login_fetch_contract:
                        c._ensure_contracts()
                    if c.activate_ca:
                        if not pid:
                            logger.warning("CA activation requested but missing SHIOAJI_PERSON_ID")
                        if not c.ca_path or not ca_pwd:
                            logger.warning("CA activation requested but missing CA_CERT_PATH/CA_PASSWORD")
                        else:
                            try:
                                start_ns = time.perf_counter_ns()
                                c.api.activate_ca(ca_path=c.ca_path, ca_passwd=ca_pwd)
                                c._record_api_latency("activate_ca", start_ns, ok=True)
                                c.ca_active = True
                                logger.info("CA activated")
                            except Exception as exc:
                                c._record_api_latency("activate_ca", start_ns, ok=False)
                                logger.error("CA activation failed", error=str(exc))
                    c.logged_in = True
                    c._last_session_refresh_ts = timebase.now_s()
                    return True

                if attempt < attempts_total:
                    retry_sleep_s = min(5.0, float(attempt))
                    logger.warning(
                        "Retrying login after failure",
                        attempt=attempt,
                        sleep_s=retry_sleep_s,
                        error=c._last_login_error,
                    )
                    time.sleep(retry_sleep_s)

            logger.error("Login retries exhausted", attempts=attempts_total, error=c._last_login_error)
            if c.metrics and hasattr(c.metrics, "shioaji_login_fail_total"):
                reason = c._sanitize_metric_label(c._last_login_error or "unknown", fallback="unknown")
                c.metrics.shioaji_login_fail_total.labels(reason=reason).inc()
            c._release_session_lock()
            return False

        if not c.api:
            logger.warning("Shioaji SDK not installed; cannot login. Staying in simulation mode.")
            return False

        logger.warning("No API key/secret found (Args/Env). Running in simulation/anonymous mode.")
        return False

    # ------------------------------------------------------------------ #
    # Session refresh (Phase-2: owned here, not in ShioajiClient)
    # ------------------------------------------------------------------ #

    def start_session_refresh_thread(self) -> None:
        """Start background thread for preventive session refresh (C3).

        Refreshes session before long holidays to prevent expiration.
        When holiday-aware mode is enabled (O4), only refreshes:
        - When approaching long holidays (days_until_trading > 1)
        - Regular interval when on trading day or day before

        Extracted from ShioajiClient._start_session_refresh_thread().
        """
        c = self._client
        if c._session_refresh_running:
            return
        if c._session_refresh_interval_s <= 0:
            return

        c._session_refresh_running = True
        c._set_thread_alive_metric("session_refresh", True)
        logger.info(
            "Starting session refresh thread",
            interval_s=c._session_refresh_interval_s,
            check_interval_s=c._session_refresh_check_interval_s,
            holiday_aware=c._session_refresh_holiday_aware,
        )

        def _refresh_loop() -> None:
            try:
                from hft_platform.core.market_calendar import get_calendar

                calendar = get_calendar()
            except ImportError:
                logger.warning("Market calendar not available for session refresh")
                c._session_refresh_running = False
                c._set_thread_alive_metric("session_refresh", False)
                return

            while c.api and c.logged_in and c._session_refresh_running:
                try:
                    time.sleep(c._session_refresh_check_interval_s)
                    if not c._session_refresh_running:
                        break

                    now = timebase.now_s()
                    now_dt = dt.datetime.now(calendar._tz)

                    # Skip refresh during active trading hours
                    if calendar.is_trading_hours(now_dt):
                        continue

                    days_until = calendar.days_until_trading(now_dt.date())
                    elapsed = now - c._last_session_refresh_ts

                    if c._session_refresh_holiday_aware:
                        # Holiday-aware mode (O4):
                        # - Refresh if approaching long holiday (days_until > 1)
                        # - Regular refresh only on trading day or day before
                        holiday_refresh = days_until > 1 and elapsed > 0
                        regular_refresh = days_until <= 1 and elapsed >= c._session_refresh_interval_s

                        if not (holiday_refresh or regular_refresh):
                            continue

                        reason = "holiday" if holiday_refresh else "regular"
                    else:
                        # Original mode: refresh based on interval only
                        if days_until > 1:
                            continue
                        if elapsed < c._session_refresh_interval_s:
                            continue
                        reason = "interval"

                    logger.info(
                        "Preventive session refresh",
                        reason=reason,
                        days_until_trading=days_until,
                        elapsed_s=round(elapsed, 0),
                    )
                    self.do_session_refresh()
                except Exception as exc:
                    logger.warning("Session refresh check failed", error=str(exc))

            c._session_refresh_running = False
            c._set_thread_alive_metric("session_refresh", False)

        c._session_refresh_thread = threading.Thread(
            target=_refresh_loop,
            name="shioaji-session-refresh",
            daemon=True,
        )
        c._session_refresh_thread.start()

    def do_session_refresh(self) -> bool:
        """Perform session refresh via logout/login cycle.

        Includes post-refresh health check (O5) to verify quotes are flowing.

        Extracted from ShioajiClient._do_session_refresh().

        Returns:
            True if refresh succeeded
        """
        c = self._client
        if not c.api:
            return False

        try:
            logger.info("Session refresh: logging out")
            start_ns = time.perf_counter_ns()
            try:
                c.api.logout()
            except Exception as exc:
                logger.warning("Session refresh logout failed", error=str(exc))

            c.logged_in = False
            c._callbacks_registered = False

            logger.info("Session refresh: logging in")
            self.login_with_retry()

            if c.logged_in:
                c._last_session_refresh_ts = timebase.now_s()
                c._record_api_latency("session_refresh", start_ns, ok=True)
                logger.info("Session refresh login successful")

                if c.tick_callback:
                    c._ensure_callbacks(c.tick_callback)
                    c._resubscribe_all()
                    c._start_quote_watchdog()

                    # Post-refresh health check (O5)
                    if c._verify_quotes_flowing():
                        logger.info("Session refresh completed, quotes flowing")
                        if c.metrics:
                            c.metrics.session_refresh_total.labels(result="ok").inc()
                        return True
                    else:
                        logger.warning("Session refresh completed but quotes not flowing")
                        if c.metrics:
                            c.metrics.session_refresh_total.labels(result="partial").inc()
                        # Still return True since login succeeded
                        return True
                else:
                    # No tick callback means no subscriptions to verify
                    if c.metrics:
                        c.metrics.session_refresh_total.labels(result="ok").inc()
                    logger.info("Session refresh completed (no subscriptions)")
                    return True
            else:
                c._record_api_latency("session_refresh", start_ns, ok=False)
                if c.metrics:
                    c.metrics.session_refresh_total.labels(result="error").inc()
                logger.error("Session refresh failed: login unsuccessful")
                return False
        except Exception as exc:
            logger.error("Session refresh failed", error=str(exc))
            if c.metrics:
                c.metrics.session_refresh_total.labels(result="error").inc()
            return False

    # ------------------------------------------------------------------ #
    # Legacy pass-through helpers
    # ------------------------------------------------------------------ #

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        return self.request_reconnect(reason=reason, force=force)

    def snapshot(self) -> SessionStateSnapshot:
        return SessionStateSnapshot(
            logged_in=bool(getattr(self._client, "logged_in", False)),
            reconnect_backoff_s=float(getattr(self._client, "_reconnect_backoff_s", 0.0)),
            last_login_error=getattr(self._client, "_last_login_error", None),
            last_reconnect_error=getattr(self._client, "_last_reconnect_error", None),
        )
