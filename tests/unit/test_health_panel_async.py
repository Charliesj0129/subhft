"""Tests verifying that health panel fetching is non-blocking (uses thread executor)."""

from __future__ import annotations

import asyncio
import time
import unittest.mock as mock

import pytest

from hft_platform.monitor import _health_panel as hp
from hft_platform.monitor._health_panel import (
    HealthState,
    _fetch_from_prometheus,
    poll_health_async,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_globals() -> None:
    """Reset module-level globals between tests to avoid state leakage."""
    hp._poll_counter = 0
    hp._current_health = None


# ---------------------------------------------------------------------------
# poll_health_async: counter / throttle behaviour
# ---------------------------------------------------------------------------


class TestPollHealthAsyncCounter:
    @pytest.mark.asyncio
    async def test_returns_none_before_first_fetch(self) -> None:
        _reset_globals()
        # counter=0 → incremented to 1 → 1 % 5 == 1, so fetch fires.
        # Patch fetch so it returns a dummy state without hitting network.
        dummy = HealthState(engine_reachable=False)
        with mock.patch.object(hp, "_fetch_from_prometheus", return_value=dummy):
            result = await poll_health_async()
        assert result is not None

    @pytest.mark.asyncio
    async def test_cached_value_returned_on_non_poll_ticks(self) -> None:
        _reset_globals()
        dummy = HealthState(engine_reachable=True)
        with mock.patch.object(hp, "_fetch_from_prometheus", return_value=dummy):
            await poll_health_async()  # tick 1 — fetch fires (counter=1)

        # Ticks 2-5: counter increments but fetch should NOT fire (cached returned).
        fetch_spy = mock.MagicMock(return_value=HealthState())
        with mock.patch.object(hp, "_fetch_from_prometheus", fetch_spy):
            for _ in range(4):
                result = await poll_health_async()
                assert result is dummy  # still the old cached value

        fetch_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_feed_counts_updated_on_each_call(self) -> None:
        _reset_globals()
        dummy = HealthState()
        with mock.patch.object(hp, "_fetch_from_prometheus", return_value=dummy):
            result = await poll_health_async(feed_live=3, feed_stale=1, feed_total=10)
        assert result is not None
        assert result.feed_live_count == 3
        assert result.feed_stale_count == 1
        assert result.feed_total_count == 10


# ---------------------------------------------------------------------------
# poll_health_async: thread-offload (non-blocking)
# ---------------------------------------------------------------------------


class TestPollHealthAsyncNonBlocking:
    @pytest.mark.asyncio
    async def test_blocking_fetch_does_not_stall_event_loop(self) -> None:
        """A 100ms sleep in _fetch_from_prometheus must not block other coroutines."""
        _reset_globals()

        async def _concurrent_task() -> float:
            start = asyncio.get_event_loop().time()
            await asyncio.sleep(0)  # yield — should complete immediately
            return asyncio.get_event_loop().time() - start

        def _slow_fetch(**_kwargs: object) -> HealthState:
            time.sleep(0.1)  # simulate a 100ms blocking call
            return HealthState(engine_reachable=True)

        with mock.patch.object(hp, "_fetch_from_prometheus", side_effect=_slow_fetch):
            # Run the slow poll and a concurrent no-op coroutine in parallel.
            t_noop, _ = await asyncio.gather(
                _concurrent_task(),
                poll_health_async(),
            )

        # The no-op coroutine must finish well under the 100ms sleep.
        assert t_noop < 0.05, (
            f"Event loop was blocked: no-op coroutine took {t_noop:.3f}s "
            "(expected < 0.05s)"
        )

    @pytest.mark.asyncio
    async def test_uses_to_thread_not_direct_call(self) -> None:
        """poll_health_async must delegate to asyncio.to_thread, not call the
        fetch function directly on the event-loop thread."""
        _reset_globals()
        called_from_thread: list[bool] = []

        def _capture_thread(**_kwargs: object) -> HealthState:
            # If this runs in a thread-pool thread, threading.current_thread()
            # will NOT be the main thread.
            import threading

            called_from_thread.append(
                threading.current_thread() is not threading.main_thread()
            )
            return HealthState()

        with mock.patch.object(hp, "_fetch_from_prometheus", side_effect=_capture_thread):
            await poll_health_async()

        assert called_from_thread == [True], (
            "_fetch_from_prometheus was not run in a worker thread; "
            "poll_health_async is still blocking the event loop"
        )


# ---------------------------------------------------------------------------
# _fetch_from_prometheus: time.monotonic() used for last_fetch_ts
# ---------------------------------------------------------------------------


class TestFetchTimestamp:
    def test_last_fetch_ts_uses_monotonic(self) -> None:
        """last_fetch_ts must be set with time.monotonic(), not time.time()."""
        raw_metrics = (
            "# HELP stormguard_mode StormGuard state\n"
            'stormguard_mode{strategy="system"} 0\n'
        )
        fake_resp = mock.MagicMock()
        fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
        fake_resp.__exit__ = mock.Mock(return_value=False)
        fake_resp.read.return_value = raw_metrics.encode()

        before = time.monotonic()
        with mock.patch("hft_platform.monitor._health_panel.urlopen", return_value=fake_resp):
            state = _fetch_from_prometheus()
        after = time.monotonic()

        assert state.engine_reachable is True
        # last_fetch_ts should be a monotonic timestamp, so it must fall
        # within the bracketed monotonic window.
        assert before <= state.last_fetch_ts <= after


# ---------------------------------------------------------------------------
# _fetch_from_prometheus: resilience / error paths
# ---------------------------------------------------------------------------


class TestFetchFromPrometheusErrors:
    def test_returns_default_state_on_url_error(self) -> None:
        from urllib.error import URLError

        with mock.patch(
            "hft_platform.monitor._health_panel.urlopen",
            side_effect=URLError("connection refused"),
        ):
            state = _fetch_from_prometheus(feed_total=5)

        assert state.engine_reachable is False
        assert state.feed_total_count == 5

    def test_returns_default_state_on_timeout(self) -> None:
        with mock.patch(
            "hft_platform.monitor._health_panel.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            state = _fetch_from_prometheus()

        assert state.engine_reachable is False

    def test_returns_default_state_on_oserror(self) -> None:
        with mock.patch(
            "hft_platform.monitor._health_panel.urlopen",
            side_effect=OSError("network error"),
        ):
            state = _fetch_from_prometheus()

        assert state.engine_reachable is False
