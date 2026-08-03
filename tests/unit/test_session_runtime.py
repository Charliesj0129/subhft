from __future__ import annotations

import datetime as _dt
import time as _real_time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji import session_runtime as session_runtime_mod
from hft_platform.feed_adapter.shioaji.session_runtime import (
    SessionRuntime,
    SessionStateSnapshot,
)

# ------------------------------------------------------------------ #
# Phase-1 delegation tests (existing)
# ------------------------------------------------------------------ #


def test_session_runtime_request_reconnect_delegates():
    client = MagicMock()
    client.reconnect.return_value = True

    runtime = SessionRuntime(client)
    ok = runtime.request_reconnect(reason="unit-test", force=True)

    assert ok is True
    client.reconnect.assert_called_once_with(reason="unit-test", force=True)


def test_session_runtime_is_logged_in_reads_client_state():
    client = MagicMock()
    client.logged_in = True
    runtime = SessionRuntime(client)

    assert runtime.is_logged_in() is True


# ------------------------------------------------------------------ #
# Phase-2: login_with_retry tests
# ------------------------------------------------------------------ #


def test_login_with_retry_no_api_key_returns_false():
    """Without SHIOAJI_API_KEY in env and no args, returns False."""
    client = MagicMock()
    client.api = MagicMock()  # SDK present but no creds

    runtime = SessionRuntime(client)
    with patch.dict("os.environ", {}, clear=False):
        # Ensure keys are not set
        import os

        for k in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"):
            os.environ.pop(k, None)
        result = runtime.login_with_retry()

    assert result is False


def test_login_with_retry_no_sdk_no_key_returns_false():
    """Without SDK and no creds, logs warning and returns False."""
    client = MagicMock()
    client.api = None

    runtime = SessionRuntime(client)
    import os

    for k in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"):
        os.environ.pop(k, None)

    result = runtime.login_with_retry()
    assert result is False


def test_login_with_retry_sdk_unavailable_with_key_returns_false():
    """With credentials but api=None, returns False."""
    client = MagicMock()
    client.api = None

    runtime = SessionRuntime(client)
    result = runtime.login_with_retry(api_key="key", secret_key="secret")
    assert result is False
    # Should NOT call _ensure_session_lock since api is None
    client._ensure_session_lock.assert_not_called()


def test_login_with_retry_success_sets_logged_in():
    """Successful login sets client.logged_in = True."""
    client = MagicMock()
    client.api = MagicMock()
    client._login_retry_max = 0
    client._login_timeout_s = 5.0
    client.fetch_contract = True
    client.subscribe_trade = True
    client.activate_ca = False
    client._last_login_error = None
    client._safe_call_with_timeout.return_value = (True, None, None, False)

    runtime = SessionRuntime(client)
    result = runtime.login_with_retry(api_key="key", secret_key="secret")

    assert result is True
    assert client.logged_in is True


def test_login_with_retry_failure_records_metric():
    """Failed login (all retries exhausted) records shioaji_login_fail_total metric."""
    client = MagicMock()
    client.api = MagicMock()
    client._login_retry_max = 0
    client._login_timeout_s = 5.0
    client.fetch_contract = False
    client.subscribe_trade = True
    client.activate_ca = False
    client._safe_call_with_timeout.return_value = (False, None, "timeout", True)
    client._sanitize_metric_label.return_value = "timeout"
    client.metrics = MagicMock()
    client.metrics.shioaji_login_fail_total = MagicMock()
    client.metrics.shioaji_login_fail_total.labels.return_value = MagicMock()

    runtime = SessionRuntime(client)
    result = runtime.login_with_retry(api_key="key", secret_key="secret")

    assert result is False
    client.metrics.shioaji_login_fail_total.labels.assert_called_once()
    client._release_session_lock.assert_called_once()


def test_login_delegates_to_login_with_retry():
    """SessionRuntime.login() is an alias for login_with_retry()."""
    client = MagicMock()
    runtime = SessionRuntime(client)

    with patch.object(SessionRuntime, "login_with_retry", return_value=True) as mock_lwr:
        result = runtime.login(api_key="k", secret_key="s")

    assert result is True
    # patch.object replaces the class attribute; instance call does NOT bind self.
    mock_lwr.assert_called_once_with(api_key="k", secret_key="s")


# ------------------------------------------------------------------ #
# Phase-2: do_session_refresh tests
# ------------------------------------------------------------------ #


def test_do_session_refresh_no_api_returns_false():
    client = MagicMock()
    client.api = None
    runtime = SessionRuntime(client)

    assert runtime.do_session_refresh() is False


def test_do_session_refresh_login_fails_returns_false():
    """If login fails after logout, do_session_refresh returns False."""
    client = MagicMock()
    client.api = MagicMock()
    client.logged_in = False  # After logout
    client.metrics = MagicMock()
    client.metrics.session_refresh_total = MagicMock()
    client.metrics.session_refresh_total.labels.return_value = MagicMock()

    runtime = SessionRuntime(client)

    with patch.object(SessionRuntime, "login_with_retry", return_value=False) as mock_lwr:
        result = runtime.do_session_refresh()

    assert result is False
    mock_lwr.assert_called_once()
    client.metrics.session_refresh_total.labels.assert_called_with(result="error")


def test_do_session_refresh_success_no_subscriptions():
    """Successful refresh with no tick_callback records ok metric."""
    client = MagicMock()
    client.api = MagicMock()
    client.tick_callback = None
    client.metrics = MagicMock()
    client.metrics.session_refresh_total = MagicMock()
    client.metrics.session_refresh_total.labels.return_value = MagicMock()

    runtime = SessionRuntime(client)

    def _set_logged_in(*args, **kwargs):
        client.logged_in = True
        return True

    with patch.object(SessionRuntime, "login_with_retry", side_effect=_set_logged_in):
        result = runtime.do_session_refresh()

    assert result is True
    client.metrics.session_refresh_total.labels.assert_called_with(result="ok")


def test_do_session_refresh_success_with_subscriptions():
    """Successful refresh with tick_callback triggers resubscription and watchdog."""
    client = MagicMock()
    client.api = MagicMock()
    client.tick_callback = MagicMock()
    client.metrics = MagicMock()
    client.metrics.session_refresh_total = MagicMock()
    client.metrics.session_refresh_total.labels.return_value = MagicMock()
    client._verify_quotes_flowing.return_value = True

    runtime = SessionRuntime(client)

    def _set_logged_in(*args, **kwargs):
        client.logged_in = True
        return True

    with patch.object(SessionRuntime, "login_with_retry", side_effect=_set_logged_in):
        result = runtime.do_session_refresh()

    assert result is True
    client._ensure_callbacks.assert_called_once_with(client.tick_callback)
    client._resubscribe_all.assert_called_once()
    client._start_quote_watchdog.assert_called_once()
    client.metrics.session_refresh_total.labels.assert_called_with(result="ok")


# ------------------------------------------------------------------ #
# Phase-2: start_session_refresh_thread tests
# ------------------------------------------------------------------ #


def test_start_session_refresh_thread_noop_if_already_running():
    client = MagicMock()
    client._session_refresh_running = True

    runtime = SessionRuntime(client)
    runtime.start_session_refresh_thread()

    # Thread should NOT be started
    client._set_thread_alive_metric.assert_not_called()


def test_start_session_refresh_thread_noop_if_interval_zero():
    client = MagicMock()
    client._session_refresh_running = False
    client._session_refresh_interval_s = 0

    runtime = SessionRuntime(client)
    runtime.start_session_refresh_thread()

    client._set_thread_alive_metric.assert_not_called()


def test_start_session_refresh_thread_starts_thread():
    client = MagicMock()
    client._session_refresh_running = False
    client._session_refresh_interval_s = 86400
    client._session_refresh_check_interval_s = 3600
    client._session_refresh_holiday_aware = False
    client._set_thread_alive_metric = MagicMock()

    runtime = SessionRuntime(client)

    with patch("hft_platform.feed_adapter.shioaji.session_runtime.threading.Thread") as mock_thread_cls:
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        with patch(
            "hft_platform.feed_adapter.shioaji.session_runtime.get_calendar",
            create=True,
        ):
            runtime.start_session_refresh_thread()

    assert client._session_refresh_running is True
    mock_thread_cls.assert_called_once()
    mock_thread.start.assert_called_once()
    assert client._session_refresh_thread is mock_thread


# ------------------------------------------------------------------ #
# Refresh-loop survival (regression: 2026-07-25 stranded facade)
# ------------------------------------------------------------------ #


def _capture_refresh_loop(client) -> object:
    """Start the refresh thread with Thread mocked out, return the loop body."""
    runtime = SessionRuntime(client)
    with patch("hft_platform.feed_adapter.shioaji.session_runtime.threading.Thread") as mock_thread_cls:
        runtime.start_session_refresh_thread()
    return runtime, mock_thread_cls.call_args.kwargs["target"]


def _loop_client(*, logged_in: bool, iterations: int) -> MagicMock:
    """Client whose refresh loop stops itself after `iterations` wake-ups.

    ``_fake_sleep`` also advances ``_now``, so the re-login backoff (which gates
    on wall-clock) behaves the way it does in production instead of collapsing
    to "every wake-up retries".
    """
    client = MagicMock()
    client.api = MagicMock()
    client.logged_in = logged_in
    client._session_refresh_running = False
    client._session_refresh_interval_s = 86400
    client._session_refresh_check_interval_s = 3600
    client._session_refresh_jitter_frac = 0.0
    client._session_relogin_poll_s = 60.0
    client._session_relogin_backoff_s = 60.0
    client._session_refresh_holiday_aware = False
    client._last_session_refresh_ts = 0.0
    client._sleep_calls = 0
    client._now = 0.0
    client._sleep_durations = []

    def _sleep(seconds: float) -> None:
        client._sleep_calls += 1
        client._sleep_durations.append(seconds)
        client._now += seconds
        if client._sleep_calls >= iterations:
            client._session_refresh_running = False

    client._fake_sleep = _sleep
    return client


@contextmanager
def _run_loop(client):
    """Patch sleep + clock for the captured refresh loop.

    The obvious spelling — ``patch("...session_runtime.time.sleep", ...)`` —
    is a trap: ``session_runtime.time`` *is* the stdlib ``time`` module, so that
    patch replaces ``time.sleep`` for the **whole process**. Any background
    thread another test left running then lands in ``_fake_sleep``, burning this
    client's wake-up budget with its own short sleeps while barely advancing the
    simulated clock. Under xdist that truncated the backoff ladder before it
    reached its cap (CI saw a final gap of 1920 s instead of 3600 s) while the
    test passed in isolation every time.

    Rebinding the module's ``time`` *name* to a stub keeps the fake clock inside
    this module, where it belongs.
    """
    stub_time = SimpleNamespace(
        sleep=client._fake_sleep,
        perf_counter_ns=_real_time.perf_counter_ns,
    )
    with (
        patch.object(session_runtime_mod, "time", stub_time),
        patch(
            "hft_platform.feed_adapter.shioaji.session_runtime.timebase.now_s",
            side_effect=lambda: client._now,
        ),
    ):
        yield


def test_refresh_loop_retries_login_when_facade_logged_out():
    """A failed refresh must not end the thread — it used to, stranding the facade.

    Before the fix `c.logged_in` sat in the while-condition, so a facade that
    failed to log back in was abandoned until the process restarted.
    """
    client = _loop_client(logged_in=False, iterations=2)
    _runtime, loop = _capture_refresh_loop(client)

    def _recover() -> bool:
        client.logged_in = True
        return True

    with (
        _run_loop(client),
        patch.object(SessionRuntime, "do_session_refresh", side_effect=_recover) as mock_refresh,
    ):
        loop()

    assert mock_refresh.call_count == 1
    assert client.logged_in is True
    client.metrics.session_refresh_total.labels.assert_any_call(result="recovered")


def test_refresh_loop_keeps_retrying_while_relogin_keeps_failing():
    client = _loop_client(logged_in=False, iterations=3)
    _runtime, loop = _capture_refresh_loop(client)

    with (
        _run_loop(client),
        patch.object(SessionRuntime, "do_session_refresh", return_value=False) as mock_refresh,
    ):
        loop()

    # Wake-ups at t=60 and t=120 each clear the backoff gate (60s then 120s);
    # the third observes the cleared stop flag and breaks.
    assert mock_refresh.call_count == 2
    assert client.logged_in is False


def test_refresh_loop_exits_when_stop_flag_cleared():
    client = _loop_client(logged_in=True, iterations=1)
    _runtime, loop = _capture_refresh_loop(client)

    with (
        _run_loop(client),
        patch.object(SessionRuntime, "do_session_refresh") as mock_refresh,
    ):
        loop()

    mock_refresh.assert_not_called()
    assert client._session_refresh_running is False
    client._set_thread_alive_metric.assert_any_call("session_refresh", False)


def test_relogin_retry_backs_off_from_seconds_not_the_check_interval():
    """A logged-out facade must not wait the full hourly check interval.

    The recovery branch originally slept ``_session_refresh_check_interval_s``
    (3600s) before its first retry, so a facade that lost its session during a
    session stayed dark for up to an hour carrying its whole symbol shard.
    """
    client = _loop_client(logged_in=False, iterations=1)
    _runtime, loop = _capture_refresh_loop(client)

    with (
        _run_loop(client),
        patch.object(SessionRuntime, "do_session_refresh", return_value=False),
    ):
        loop()

    assert client._sleep_durations == [60.0]
    assert max(client._sleep_durations) < client._session_refresh_check_interval_s


def test_relogin_backoff_caps_at_check_interval():
    """Doubling must stop at the check interval, never run away past it."""
    client = _loop_client(logged_in=False, iterations=400)
    _runtime, loop = _capture_refresh_loop(client)

    attempt_times: list[float] = []

    def _fail() -> bool:
        attempt_times.append(client._now)
        return False

    with (
        _run_loop(client),
        patch.object(SessionRuntime, "do_session_refresh", side_effect=_fail),
    ):
        loop()

    gaps = [b - a for a, b in zip(attempt_times, attempt_times[1:])]
    assert gaps[0] == pytest.approx(60.0)
    assert max(gaps) <= client._session_refresh_check_interval_s
    # Late gaps have saturated at the cap rather than continuing to double.
    assert gaps[-1] == pytest.approx(client._session_refresh_check_interval_s)


def test_refresh_loop_clock_stub_does_not_leak_into_the_global_time_module():
    """The loop's fake clock must not replace ``time.sleep`` process-wide.

    ``session_runtime.time`` is the stdlib module object, so patching
    ``session_runtime.time.sleep`` mutates it for every thread in the process.
    A background thread left running by an earlier test then consumes this
    client's wake-up budget, which is how ``test_relogin_backoff_caps_at_check_interval``
    passed alone and failed under xdist. Assert the isolation directly rather
    than trusting it.
    """
    client = _loop_client(logged_in=False, iterations=1)
    real_sleep = _real_time.sleep

    with _run_loop(client):
        assert _real_time.sleep is real_sleep
        # A foreign thread's sleep must not be attributed to this client.
        _real_time.sleep(0)
        assert client._sleep_calls == 0
        assert session_runtime_mod.time.sleep is client._fake_sleep

    assert _real_time.sleep is real_sleep


def test_relogin_backoff_resets_after_successful_recovery():
    """A recovered facade that logs out again retries from the base delay."""
    client = _loop_client(logged_in=False, iterations=6)
    _runtime, loop = _capture_refresh_loop(client)

    attempt_times: list[float] = []
    outcomes = [False, False, True]

    def _refresh() -> bool:
        attempt_times.append(client._now)
        ok = outcomes.pop(0) if outcomes else False
        client.logged_in = ok
        return ok

    # The facade loses its session again shortly after recovering.
    base_sleep = client._fake_sleep

    def _sleep(seconds: float) -> None:
        base_sleep(seconds)
        if client._now >= 300.0:
            client.logged_in = False

    client._fake_sleep = _sleep

    with (
        _run_loop(client),
        patch.object(SessionRuntime, "do_session_refresh", side_effect=_refresh),
    ):
        loop()

    assert attempt_times == [60.0, 120.0, 240.0, 300.0]
    # Backoff had grown to 120s before the success at t=240; afterwards the
    # next logged-out wake-up retries at the base poll instead of waiting 240s.
    assert attempt_times[2] - attempt_times[1] == 120.0
    assert attempt_times[3] - attempt_times[2] == 60.0


# ------------------------------------------------------------------ #
# Snapshot helper
# ------------------------------------------------------------------ #


def test_snapshot_returns_correct_fields():
    client = MagicMock()
    client.logged_in = True
    client._reconnect_backoff_s = 30.0
    client._last_login_error = "timeout"
    client._last_reconnect_error = None

    runtime = SessionRuntime(client)
    snap = runtime.snapshot()

    assert isinstance(snap, SessionStateSnapshot)
    assert snap.logged_in is True
    assert snap.reconnect_backoff_s == 30.0
    assert snap.last_login_error == "timeout"
    assert snap.last_reconnect_error is None


# ------------------------------------------------------------------ #
# Cross-facade login staggering (2026-07-24 "451 Too Many Connections")
# ------------------------------------------------------------------ #


def _refresh_client() -> MagicMock:
    client = MagicMock()
    client.api = MagicMock()
    client.tick_callback = None
    client.metrics = MagicMock()
    client.metrics.session_refresh_total.labels.return_value = MagicMock()
    return client


def test_session_refresh_holds_login_slot_across_logout_and_login():
    """The slot must span logout->login: that whole window holds a connection."""
    client = _refresh_client()
    client.api.logout.side_effect = lambda: events.append("logout")
    events: list[str] = []

    runtime = SessionRuntime(client)

    with (
        patch(
            "hft_platform.feed_adapter.shioaji.session_runtime.acquire_login_slot",
            side_effect=lambda **_: events.append("acquire") or True,
        ),
        patch(
            "hft_platform.feed_adapter.shioaji.session_runtime.release_login_slot",
            side_effect=lambda: events.append("release"),
        ),
        patch.object(SessionRuntime, "login_with_retry", side_effect=lambda: events.append("login")),
    ):
        runtime.do_session_refresh()

    assert events == ["acquire", "logout", "login", "release"]


def test_session_refresh_releases_login_slot_when_login_raises():
    """A raising login must not strand the slot and wedge every other facade."""
    client = _refresh_client()
    runtime = SessionRuntime(client)

    with (
        patch(
            "hft_platform.feed_adapter.shioaji.session_runtime.acquire_login_slot",
            return_value=True,
        ),
        patch("hft_platform.feed_adapter.shioaji.session_runtime.release_login_slot") as mock_release,
        patch.object(SessionRuntime, "login_with_retry", side_effect=RuntimeError("login blew up")),
    ):
        assert runtime.do_session_refresh() is False

    mock_release.assert_called_once()


def test_session_refresh_proceeds_unserialised_when_slot_times_out():
    """Timing out on the slot must still refresh — a stale session is worse."""
    client = _refresh_client()
    runtime = SessionRuntime(client)

    def _login() -> bool:
        # do_session_refresh clears logged_in before logging in; a real login
        # sets it back, so the mock has to as well or the refresh reads as failed.
        client.logged_in = True
        return True

    with (
        patch(
            "hft_platform.feed_adapter.shioaji.session_runtime.acquire_login_slot",
            return_value=False,
        ),
        patch("hft_platform.feed_adapter.shioaji.session_runtime.release_login_slot") as mock_release,
        patch.object(SessionRuntime, "login_with_retry", side_effect=_login) as mock_login,
    ):
        assert runtime.do_session_refresh() is True

    mock_login.assert_called_once()
    client.api.logout.assert_called_once()
    # Never release a slot we do not hold: that would free another facade's.
    mock_release.assert_not_called()


def test_session_refresh_passes_configured_stagger_settings_to_slot():
    client = _refresh_client()
    client._session_refresh_stagger_gap_s = 7.5
    client._session_refresh_stagger_timeout_s = 90.0
    runtime = SessionRuntime(client)

    with (
        patch(
            "hft_platform.feed_adapter.shioaji.session_runtime.acquire_login_slot",
            return_value=True,
        ) as mock_acquire,
        patch("hft_platform.feed_adapter.shioaji.session_runtime.release_login_slot"),
        patch.object(SessionRuntime, "login_with_retry", return_value=True),
    ):
        runtime.do_session_refresh()

    kwargs = mock_acquire.call_args.kwargs
    assert kwargs["min_gap_s"] == 7.5
    assert kwargs["timeout_s"] == 90.0


# ------------------------------------------------------------------ #
# Contract-aware login timeout, SDK-busy backoff, bounded failure label
#
# Regression for the daily 08:30 CST cascade on THESHOW: the contract-fetching
# login exceeded a 20 s budget, its worker was abandoned inside shioaji 1.5.x's
# Rust `_core`, and the immediate fallback re-entered the same object and failed
# with "Already borrowed" in under 3 ms — burning the retry ladder and escalating
# into broker 451s.
# ------------------------------------------------------------------ #


def _login_client(**overrides):
    client = MagicMock()
    client.api = MagicMock()
    client._login_retry_max = 1
    client._login_timeout_s = 20.0
    client._login_contract_timeout_s = 60.0
    client.fetch_contract = True
    client.subscribe_trade = True
    client.activate_ca = False
    client._last_login_error = None
    client.metrics = None
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


def test_login_uses_contract_timeout_when_fetching_contracts():
    client = _login_client()
    client._safe_call_with_timeout.return_value = (True, None, None, False)

    assert SessionRuntime(client).login_with_retry(api_key="k", secret_key="s") is True

    op, _fn, timeout_s = client._safe_call_with_timeout.call_args[0]
    assert op == "login"
    assert timeout_s == 60.0


def test_login_uses_tight_timeout_when_not_fetching_contracts():
    """A quote-only login does no contract work, so it keeps the short budget."""
    client = _login_client(fetch_contract=False)
    client._safe_call_with_timeout.return_value = (True, None, None, False)

    assert SessionRuntime(client).login_with_retry(api_key="k", secret_key="s") is True

    _op, _fn, timeout_s = client._safe_call_with_timeout.call_args[0]
    assert timeout_s == 20.0


def test_login_contract_timeout_never_below_the_base_timeout():
    """A misconfigured contract budget must not shorten the login."""
    client = _login_client(_login_timeout_s=45.0, _login_contract_timeout_s=10.0)
    client._safe_call_with_timeout.return_value = (True, None, None, False)

    SessionRuntime(client).login_with_retry(api_key="k", secret_key="s")

    _op, _fn, timeout_s = client._safe_call_with_timeout.call_args[0]
    assert timeout_s == 45.0


def test_login_skips_fallback_and_backs_off_when_sdk_busy():
    """SDKBusyError must not trigger the no-contract fallback or a retry.

    Both would re-enter the same SDK object and fail instantly with
    "Already borrowed", which is exactly how one slow login turned into 451s.
    """
    from hft_platform.feed_adapter.shioaji._infra import SDKBusyError

    client = _login_client()
    client._safe_call_with_timeout.return_value = (
        False,
        None,
        SDKBusyError("login", "login"),
        True,
    )

    assert SessionRuntime(client).login_with_retry(api_key="k", secret_key="s") is False

    ops = [call[0][0] for call in client._safe_call_with_timeout.call_args_list]
    assert ops == ["login"], f"expected a single attempt, got {ops}"
    assert client.logged_in is not True


def test_login_skips_fallback_when_the_sdk_itself_reports_already_borrowed():
    """The busy signal also arrives as a plain SDK error, not only SDKBusyError.

    The InflightGuard only knows about workers *we* abandoned. On 2026-07-30
    04:12 CST all four facades' solace sessions dropped at once, the SDK went
    busy inside its own reconnect, and ``Already borrowed`` came straight back
    from ``_core``. Only ``SDKBusyError`` was handled, so the ladder drove the
    no-contract fallback and both retries into the same busy object: 165 instant
    failures and 6 exhausted ladders in 90 s off one network blip.
    """
    client = _login_client()
    client._safe_call_with_timeout.return_value = (
        False,
        None,
        RuntimeError("Already borrowed"),
        False,
    )

    assert SessionRuntime(client).login_with_retry(api_key="k", secret_key="s") is False

    ops = [call[0][0] for call in client._safe_call_with_timeout.call_args_list]
    assert ops == ["login"], f"expected one attempt and no fallback, got {ops}"


def test_login_still_falls_back_for_a_failure_that_is_not_sdk_busy():
    """The busy check must not swallow ordinary failures the fallback can fix."""
    client = _login_client()
    client._safe_call_with_timeout.return_value = (
        False,
        None,
        RuntimeError("login: contract download stalled"),
        True,
    )

    assert SessionRuntime(client).login_with_retry(api_key="k", secret_key="s") is False

    ops = [call[0][0] for call in client._safe_call_with_timeout.call_args_list]
    assert "login_fallback" in ops, f"expected the no-contract fallback to run, got {ops}"


def test_login_records_contract_freshness_when_login_fetched_contracts():
    """A successful contract-fetching login is the only real freshness signal.

    ``fetch_contracts`` on a logged-in facade fails 100% of the time on shioaji
    1.5.6, so ``contract_cache_last_success_ts`` sat at 0.0 forever and a
    staleness alert could never fire.
    """
    gauge = MagicMock()
    metrics = MagicMock()
    metrics.contract_cache_last_success_ts = gauge
    client = _login_client(metrics=metrics)
    client._safe_call_with_timeout.return_value = (True, None, None, False)

    assert SessionRuntime(client).login_with_retry(api_key="k", secret_key="s") is True

    assert gauge.set.call_count == 1
    assert gauge.set.call_args[0][0] > 0


def test_login_does_not_record_contract_freshness_for_a_quote_only_connection():
    """A quote-only login fetches no contracts, so it must not claim freshness."""
    gauge = MagicMock()
    metrics = MagicMock()
    metrics.contract_cache_last_success_ts = gauge
    client = _login_client(fetch_contract=False, metrics=metrics)
    client._safe_call_with_timeout.return_value = (True, None, None, False)

    assert SessionRuntime(client).login_with_retry(api_key="k", secret_key="s") is True

    gauge.set.assert_not_called()


def test_login_failure_reason_label_stays_bounded_across_distinct_errors():
    """Raw broker errors carry a per-request id; the label must not."""
    from hft_platform.feed_adapter.shioaji.session_runtime import classify_login_failure

    reasons = {
        classify_login_failure(
            "login: request #P2P/v:host/AAA/PYAPI/x/0728/1/LOGINING/_ code: 451, detail: Too Many Connections."
        ),
        classify_login_failure(
            "login: request #P2P/v:host/BBB/PYAPI/y/0729/2/LOGINING/_ code: 451, detail: Too Many Connections."
        ),
    }
    assert reasons == {"connection_limit"}

    assert classify_login_failure("Already borrowed") == "sdk_busy"
    assert classify_login_failure("login timed out after 20.0s") == "timeout"
    assert classify_login_failure(None) == "unknown"
    assert classify_login_failure("something else entirely") == "other"


def test_login_failure_metric_uses_the_bounded_reason():
    client = _login_client(_login_retry_max=0)
    client.metrics = MagicMock()
    client._safe_call_with_timeout.return_value = (
        False,
        None,
        RuntimeError("login: request #P2P/v:host/AAA/0729/9/LOGINING/_ code: 451, detail: Too Many Connections."),
        False,
    )

    assert SessionRuntime(client).login_with_retry(api_key="k", secret_key="s") is False

    client.metrics.shioaji_login_fail_total.labels.assert_called_once_with(reason="connection_limit")


# ------------------------------------------------------------------ #
# Preventive refresh scheduling (regression: 2026-08-02 residual 451s)
# ------------------------------------------------------------------ #


class _StubCalendar:
    """Calendar that records which market it was asked about."""

    _tz = _dt.timezone(_dt.timedelta(hours=8))

    def __init__(self, *, stock_open: bool, futures_open: bool, days_until: int) -> None:
        self.product_types: list[str | None] = []
        self._stock_open = stock_open
        self._futures_open = futures_open
        self._days_until = days_until

    def is_trading_hours(self, ts, product_type=None) -> bool:
        self.product_types.append(product_type)
        return self._futures_open if product_type in ("future", "option") else self._stock_open

    def days_until_trading(self, _date) -> int:
        return self._days_until


def _preventive_client(*, holiday_aware: bool, elapsed_s: float) -> MagicMock:
    """A logged-in client whose first wake-up lands on the schedule boundary."""
    client = _loop_client(logged_in=True, iterations=2)
    client._session_refresh_holiday_aware = holiday_aware
    # One wake-up must cross next_schedule_ts (= now + check_interval).
    client._session_relogin_poll_s = 3600.0
    client._last_session_refresh_ts = 3600.0 - elapsed_s
    return client


def _run_preventive(client, calendar):
    _runtime, loop = _capture_refresh_loop(client)
    with (
        _run_loop(client),
        patch("hft_platform.core.market_calendar.get_calendar", return_value=calendar),
        patch.object(SessionRuntime, "do_session_refresh", return_value=True) as mock_refresh,
    ):
        loop()
    return mock_refresh


def test_preventive_refresh_asks_the_calendar_about_futures_not_stocks():
    """The default calendar window is TWSE stocks; this platform trades TAIFEX."""
    client = _preventive_client(holiday_aware=False, elapsed_s=90_000.0)
    calendar = _StubCalendar(stock_open=False, futures_open=False, days_until=0)

    _run_preventive(client, calendar)

    assert calendar.product_types == ["future"]


def test_preventive_refresh_is_skipped_during_the_futures_night_session():
    """Logging a live facade out mid-session is what produced the residual 451s.

    The night session (15:00-05:00) is entirely outside the TWSE window, so
    asking the calendar the default question answered "closed" and a
    logout/login cycle ran on facades carrying 74 live subscriptions.
    """
    client = _preventive_client(holiday_aware=False, elapsed_s=90_000.0)
    calendar = _StubCalendar(stock_open=False, futures_open=True, days_until=0)

    mock_refresh = _run_preventive(client, calendar)

    assert mock_refresh.call_count == 0


def test_preventive_refresh_runs_once_the_futures_session_is_closed():
    client = _preventive_client(holiday_aware=False, elapsed_s=90_000.0)
    calendar = _StubCalendar(stock_open=False, futures_open=False, days_until=0)

    mock_refresh = _run_preventive(client, calendar)

    assert mock_refresh.call_count == 1


def test_holiday_refresh_honours_the_refresh_interval():
    """ "Approaching a long holiday" must not mean "every check interval".

    ``days_until > 1 and elapsed > 0`` is true on every pass, so all four
    facades relogged in hourly for the whole weekend — 96 refreshes in 60 h on
    THESHOW, every one reason="holiday".
    """
    client = _preventive_client(holiday_aware=True, elapsed_s=3_600.0)
    calendar = _StubCalendar(stock_open=False, futures_open=False, days_until=2)

    mock_refresh = _run_preventive(client, calendar)

    assert mock_refresh.call_count == 0


def test_holiday_refresh_still_happens_across_a_long_break():
    """The interval gate must not disable holiday refresh, only rate-limit it."""
    client = _preventive_client(holiday_aware=True, elapsed_s=90_000.0)
    calendar = _StubCalendar(stock_open=False, futures_open=False, days_until=2)

    mock_refresh = _run_preventive(client, calendar)

    assert mock_refresh.call_count == 1
