from __future__ import annotations

import datetime as dt
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.feed_adapter.shioaji._infra import (
    SDKBusyError,
    acquire_login_slot,
    client_float,
    refresh_sleep_s,
    release_login_slot,
    scrub_broker_error,
)

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji.client import ShioajiClient

logger = get_logger("feed_adapter.session_runtime")


def _is_connection_limit_error(error: str | None) -> bool:
    if not error:
        return False
    normalized = error.lower()
    return "too many connections" in normalized or "status_code=451" in normalized or "status code 451" in normalized


#: Bounded label values for ``shioaji_login_fail_total{reason=...}``.
#: The broker's error strings embed a per-request routing id, so feeding them to
#: a Prometheus label mints a new series per failure — cardinality that explodes
#: exactly when the system is already failing, and which also publishes broker
#: session identifiers into ``/metrics``. Classify instead of pasting.
_LOGIN_FAILURE_REASONS = ("connection_limit", "sdk_busy", "timeout", "auth", "network", "unknown", "other")


def _reconnect_in_flight(client: Any) -> bool:
    """True while another thread holds this facade's reconnect lock.

    Probe-and-release, the same non-blocking pattern ``quote_runtime`` uses:
    the acquire only succeeds when nobody else holds the lock and we hand it
    straight back, so this is a read of the lock, never a claim on it.
    """
    lock = getattr(client, "_reconnect_lock", None)
    if lock is None:
        return False
    if not lock.acquire(blocking=False):
        return True
    lock.release()
    return False


def classify_login_failure(error: str | None) -> str:
    """Map a raw broker login error onto a bounded reason label."""
    if not error:
        return "unknown"
    normalized = error.lower()
    if _is_connection_limit_error(normalized):
        return "connection_limit"
    if "already borrowed" in normalized or "sdk still occupied" in normalized:
        return "sdk_busy"
    if "timed out" in normalized or "timeout" in normalized:
        return "timeout"
    auth_tokens = ("unauthorized", "forbidden", "invalid api", "authentication", "401", "403")
    if any(token in normalized for token in auth_tokens):
        return "auth"
    if any(token in normalized for token in ("connection", "network", "unreachable", "refused", "reset", "dns")):
        return "network"
    return "other"


def _login_timeout_for(c: Any, *, fetch_contract: bool) -> float:
    """Return the login timeout budget for this attempt.

    Fetching contracts adds the contract download and parse on top of the login
    handshake — ``contracts_timeout`` alone defaults to 10 s — so it gets a
    separate, longer budget. The no-contract fallback does none of that work and
    keeps the tight ``login_timeout_s``, so a genuinely wedged login is still
    caught quickly.
    """
    if not fetch_contract:
        return client_float(c, "_login_timeout_s", 20.0)
    return max(
        client_float(c, "_login_timeout_s", 20.0),
        client_float(c, "_login_contract_timeout_s", 60.0),
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


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
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            return False

    def is_logged_in(self) -> bool:
        return bool(getattr(self._client, "logged_in", False))

    # ------------------------------------------------------------------ #
    # Login lifecycle (Phase-2: owned here, not in ShioajiClient)
    # ------------------------------------------------------------------ #

    def login(self, *args, **kwargs) -> bool:
        """Public entrypoint — login_with_retry plus a bounded backoff-retry
        when the broker rejects on connection limit (451).

        After an engine restart the broker can hold the previous session's
        slots for ~60s; failing startup immediately turns into a container
        crash-loop that keeps re-consuming sessions. Waiting out the release
        window makes restart-in-place safe. Runs on the startup/reconnect
        thread, never on the event loop, so the blocking sleep is acceptable.
        """
        if self.login_with_retry(*args, **kwargs):
            return True
        max_retries = _env_int("HFT_LOGIN_CONNLIMIT_RETRIES", 2)
        backoff_s = _env_float("HFT_LOGIN_CONNLIMIT_BACKOFF_S", 75.0)
        for retry in range(1, max_retries + 1):
            if not _is_connection_limit_error(getattr(self._client, "_last_login_error", None)):
                break
            logger.warning(
                "login_connection_limit_backoff",
                sleep_s=backoff_s,
                retry=retry,
                max_retries=max_retries,
            )
            time.sleep(backoff_s)
            if self.login_with_retry(*args, **kwargs):
                return True
        return False

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
                    _login_timeout_for(c, fetch_contract=login_fetch_contract),
                )
                c._record_api_latency("login", start_ns, ok=ok)
                if not ok:
                    c._last_login_error = scrub_broker_error(err) if err is not None else "unknown"
                    # Two independent sources of "the SDK is busy, come back
                    # later", and both must skip the fallback and the ladder:
                    #
                    #   SDKBusyError  - the InflightGuard knows a worker *we*
                    #                   abandoned is still inside the SDK.
                    #   "sdk_busy"    - the SDK itself refused re-entry. The
                    #                   guard cannot see this one: the borrow is
                    #                   held by shioaji's own internals, not by
                    #                   an abandoned call of ours.
                    #
                    # Only the first was handled, so the second fell through to
                    # the no-contract fallback and both retries — all re-entering
                    # the same busy object and failing instantly. Observed
                    # 2026-07-30 04:12 CST: every facade's solace session dropped
                    # at once, the SDK went busy inside its own reconnect, and the
                    # ladder turned one network blip into 165 "Already borrowed"
                    # failures and 6 exhausted ladders in 90 s.
                    if isinstance(err, SDKBusyError) or classify_login_failure(c._last_login_error) == "sdk_busy":
                        logger.warning(
                            "Login deferred: broker SDK still occupied by an earlier call",
                            attempt=attempt,
                            blocking_op=getattr(err, "blocking_op", None),
                        )
                        break
                    if _is_connection_limit_error(c._last_login_error):
                        logger.error(
                            "Login rejected by broker connection limit; skipping fallback and immediate retry",
                            attempt=attempt,
                            error=c._last_login_error,
                        )
                        break
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
                            _login_timeout_for(c, fetch_contract=False),
                        )
                        c._record_api_latency("login", start_ns, ok=ok_fb)
                        if ok_fb:
                            login_fetch_contract = False
                            # NOTE: do NOT mutate c.fetch_contract here.
                            # The local flag `login_fetch_contract` is enough to track
                            # that this attempt skipped contract fetch.  Mutating the
                            # persistent c.fetch_contract to False would prevent the
                            # post-login _ensure_contracts() call at line 196 from
                            # running, leaving contracts_ready=False permanently.
                            ok = True
                        else:
                            c._last_login_error = scrub_broker_error(err_fb) if err_fb is not None else "unknown"
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
                    if login_fetch_contract:
                        # The only contract load on this platform that actually
                        # works. ``fetch_contracts`` on a logged-in facade fails
                        # 100% of the time on shioaji 1.5.6 — its Rust ``_core``
                        # wants exclusive ownership and our 74 live
                        # subscriptions hold references for the whole session —
                        # so the hourly refresh never writes the cache and
                        # ``contract_cache_last_success_ts`` sat at 0.0 forever,
                        # which in turn made a staleness alert unable to fire.
                        # Login-time freshness is real freshness; record it.
                        #
                        # Deliberately keyed on ``login_fetch_contract``, NOT on
                        # ``contracts_ready`` — the latter is
                        # ``hasattr(api, "Contracts")``, always True once logged
                        # in, and trusting it is the original bug.
                        try:
                            gauge = getattr(c.metrics, "contract_cache_last_success_ts", None) if c.metrics else None
                            if gauge is not None:
                                gauge.set(timebase.now_s())
                        except Exception as exc:  # a metric must never break login
                            logger.debug("operation_fallback", error=str(exc))
                    if not login_fetch_contract and c.fetch_contract:
                        c._ensure_contracts()
                    # Verify contracts regardless of which login path was used.
                    contracts_ok = c.contracts_ready
                    c._contracts_ready = contracts_ok
                    if contracts_ok:
                        logger.info("Contracts loaded", ready=True)
                    elif c.fetch_contract:
                        # This connection was configured to fetch contracts but
                        # contracts_ready is still False — orders will be blocked.
                        logger.error(
                            "Contracts not available after login — order placement will be blocked",
                            fetch_contract_attempted=login_fetch_contract,
                            ready=False,
                        )
                    else:
                        # Quote-only connection (fetch_contract=False): contracts are
                        # intentionally not fetched (e.g. QuoteConnectionPool group_id>0
                        # skips contracts to save ~27 MB per connection).
                        # contracts_ready=False is expected here — not an error.
                        logger.debug(
                            "Contracts not fetched (quote-only connection)",
                            fetch_contract_configured=False,
                            ready=False,
                        )
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
                                logger.error("CA activation failed", error=scrub_broker_error(exc))
                    c.logged_in = True
                    c._last_session_refresh_ts = timebase.now_s()
                    c._release_session_lock()
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
                c.metrics.shioaji_login_fail_total.labels(reason=classify_login_failure(c._last_login_error)).inc()
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

            # `c.logged_in` is deliberately NOT part of this condition. A
            # refresh that logs out and then fails to log back in (broker
            # "code: 451, Too Many Connections", transient network) leaves it
            # False, so having it here terminated the thread on the first
            # failure and abandoned the facade until some unrelated path
            # happened to restart it. On 2026-07-25 that stranded 1 of 4 quote
            # facades for 24 h, silently dropping its 74 symbols through an
            # entire night session while FeedState stayed CONNECTED.
            check_interval_s = client_float(c, "_session_refresh_check_interval_s", 3600.0)
            base_backoff_s = client_float(c, "_session_relogin_backoff_s", 60.0)
            poll_s = min(client_float(c, "_session_relogin_poll_s", 60.0), check_interval_s)
            jitter_frac = client_float(c, "_session_refresh_jitter_frac", 0.15)

            relogin_attempts = 0
            relogin_backoff_s = 0.0
            relogin_next_ts = 0.0
            # How long this thread will stand aside for a reconnect that is
            # already in flight. Reusing the re-login backoff keeps one number
            # for "how long am I willing to wait before trying myself".
            max_defer_s = base_backoff_s
            defer_started_ts = 0.0
            # Start the schedule clock a full interval out so the poll quantum
            # cannot pull the first refresh evaluation forward.
            next_schedule_ts = timebase.now_s() + check_interval_s

            while c.api and c._session_refresh_running:
                try:
                    # Jitter each wake-up so facades brought up together by the
                    # pool do not stay phase-aligned and re-login in lockstep.
                    time.sleep(refresh_sleep_s(poll_s, jitter_frac, random.random))
                    if not c._session_refresh_running:
                        break

                    now = timebase.now_s()

                    if not c.logged_in:
                        # Recovery path. do_session_refresh() rebuilds the whole
                        # facade (login -> callbacks -> resubscribe -> watchdog)
                        # and already serialises on the process-wide login slot,
                        # so retrying here cannot re-create the login storm that
                        # caused the logout.
                        #
                        # But `logged_in` is also False for the whole
                        # logout->login gap of a reconnect running on another
                        # thread (reconnect_orchestrator clears it before
                        # calling login()). Reading that as "abandoned facade"
                        # stacks a second logout/login onto the first, and the
                        # broker answers 451 — 8 of them inside the 14:45 CST
                        # pre-open window on 2026-08-07. Stand aside instead,
                        # without burning an attempt or advancing the backoff:
                        # this is somebody else's recovery, not a failed one.
                        #
                        # Bounded on purpose. A reconnect that never releases
                        # the lock must not disable this recovery path for
                        # good — that would be the 2026-07-25 stranded-facade
                        # failure wearing a new costume.
                        if _reconnect_in_flight(c):
                            if defer_started_ts == 0.0:
                                defer_started_ts = now
                                logger.info("Session refresh deferring to an in-flight reconnect")
                            if now - defer_started_ts < max_defer_s:
                                continue
                            logger.warning(
                                "In-flight reconnect outlasted the defer budget; recovering anyway",
                                deferred_s=round(now - defer_started_ts, 1),
                            )
                        defer_started_ts = 0.0
                        if now < relogin_next_ts:
                            continue
                        relogin_attempts += 1
                        logger.warning(
                            "Session refresh thread found facade logged out; retrying login",
                            attempt=relogin_attempts,
                        )
                        if self.do_session_refresh():
                            logger.info(
                                "Facade recovered after logged-out gap",
                                attempts=relogin_attempts,
                            )
                            if c.metrics:
                                c.metrics.session_refresh_total.labels(result="recovered").inc()
                            relogin_attempts = 0
                            relogin_backoff_s = 0.0
                            relogin_next_ts = 0.0
                        else:
                            relogin_backoff_s = min(
                                relogin_backoff_s * 2.0 if relogin_backoff_s else base_backoff_s,
                                check_interval_s,
                            )
                            relogin_next_ts = timebase.now_s() + relogin_backoff_s
                        continue

                    relogin_attempts = 0
                    relogin_backoff_s = 0.0
                    relogin_next_ts = 0.0

                    # Everything below is the preventive-refresh schedule, which
                    # still runs once per check interval regardless of poll rate.
                    if now < next_schedule_ts:
                        continue
                    next_schedule_ts = now + check_interval_s

                    now_dt = dt.datetime.fromtimestamp(timebase.now_s(), tz=calendar._tz)

                    # Skip refresh during active trading hours.
                    #
                    # ``product_type="future"`` is load-bearing: the default is
                    # the TWSE *stock* window (09:00-13:30), and this platform
                    # trades TAIFEX. Without it the entire night session
                    # (15:00-05:00) reads as "closed", so a preventive
                    # logout/login cycle ran on facades carrying 74 live
                    # subscriptions while quotes were flowing. Measured on
                    # THESHOW: refreshes at 00:11, 01:11, ... CST on 2026-08-01,
                    # i.e. inside the Friday night session, and every observed
                    # residual "451 Too Many Connections" outside a restart sat
                    # in that window.
                    if calendar.is_trading_hours(now_dt, product_type="future"):
                        continue

                    days_until = calendar.days_until_trading(now_dt.date())
                    elapsed = now - c._last_session_refresh_ts

                    if c._session_refresh_holiday_aware:
                        # Holiday-aware mode (O4):
                        # - Refresh during a long break (days_until > 1) too, so
                        #   the session does not expire across it
                        # - Regular refresh only on trading day or day before
                        #
                        # Both branches honour the interval. ``elapsed > 0`` used
                        # to stand in for the holiday branch's condition, which
                        # is true on every pass, so "refresh when approaching a
                        # long holiday" actually meant "relogin every facade once
                        # per check interval for the whole break": 96 refreshes
                        # in 60 h on THESHOW, all reason="holiday", exactly 4 per
                        # hour. That churn is what fed the residual 451s.
                        holiday_refresh = days_until > 1 and elapsed >= c._session_refresh_interval_s
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

        # Hold the process-wide login slot across the whole logout/login cycle:
        # the broker counts concurrent connections, so two facades overlapping
        # here is what produced "code: 451, detail: Too Many Connections".
        # A timeout returns False and we refresh anyway — a stale session is a
        # worse failure than a 451 the retry path already recognises.
        slot_held = acquire_login_slot(
            min_gap_s=client_float(c, "_session_refresh_stagger_gap_s", 5.0),
            timeout_s=client_float(c, "_session_refresh_stagger_timeout_s", 120.0),
            metrics=c.metrics,
        )
        try:
            try:
                logger.info("Session refresh: logging out", serialised=slot_held)
                start_ns = time.perf_counter_ns()
                try:
                    c.api.logout()
                except Exception as exc:
                    logger.warning("Session refresh logout failed", error=scrub_broker_error(exc))

                c.logged_in = False
                c._callbacks_registered = False

                logger.info("Session refresh: logging in")
                self.login_with_retry()
            finally:
                # Released as soon as the connection is (re)established — the
                # resubscribe and quote-verification below no longer contend for
                # a broker connection slot, so holding it there would serialise
                # the whole pool behind one facade's 10 s verify timeout.
                if slot_held:
                    release_login_slot()

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
            logger.error("Session refresh failed", error=scrub_broker_error(exc))
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
