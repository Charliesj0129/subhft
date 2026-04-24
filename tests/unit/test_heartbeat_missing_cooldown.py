"""Heartbeat-missing log cooldown + off-hours gate.

Regression: prod emitted 3302 ``Heartbeat missing`` warnings/24h during a single
65-minute overnight outage (no cooldown, no trading-hours gate).
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.services._md_ingestion import FeedState
from hft_platform.services._md_reconnect import MarketDataReconnectMixin


class _FakeMD(MarketDataReconnectMixin):
    def __init__(self) -> None:
        self.running = True
        self.state = FeedState.CONNECTED
        self.last_event_ts: float = 0.0
        self.last_event_mono: float = 0.0
        self._last_rollover_reconnect_date: dt.date | None = None
        self._last_rollover_seen_date: dt.date | None = None
        self._pending_reconnect_reason: str | None = None
        self._pending_reconnect_gap: float = 0.0
        self._pending_reconnect_since: float | None = None
        self._last_resubscribe_ts: float = 0.0
        self._last_reconnect_ts: float = 0.0
        self._resubscribe_attempts: int = 0
        self._reconnect_tzinfo: dt.tzinfo = dt.timezone.utc
        self.reconnect_days: set[str] = set()
        self.reconnect_hours: str = ""
        self.reconnect_hours_2: str = ""
        self.resubscribe_cooldown_s: float = 15.0
        self.reconnect_cooldown_s: float = 60.0
        self.heartbeat_threshold_s: float = 5.0
        self.resubscribe_gap_s: float = 15.0
        self.force_reconnect_gap_s: float = 300.0
        self.reconnect_gap_s: float = 60.0
        self.reconnect_timeout_s: float = 5.0
        self.metrics_registry = None
        self.client = None
        self._market_open_grace_s: float = 0.0
        # Heartbeat rate limit state
        self._last_heartbeat_missing_log_ts: float = 0.0
        self._last_heartbeat_off_hours_log_ts: float = 0.0
        self._heartbeat_log_cooldown_s: float = 60.0
        self._heartbeat_skip_off_hours: bool = True
        self._heartbeat_off_hours_log_interval_s: float = 300.0


@pytest.fixture()
def md() -> _FakeMD:
    return _FakeMD()


@pytest.mark.asyncio
async def test_heartbeat_warning_suppressed_within_cooldown(md):
    """Second call within 60s must not re-emit the warning."""
    md.last_event_mono = 100.0
    md.last_event_ts = 1.0
    # Stub resubscribe/reconnect to avoid side effects
    md._attempt_resubscribe = AsyncMock()
    md._request_reconnect = AsyncMock()
    md._should_rollover_reconnect = MagicMock(return_value=False)
    md._is_trading_hours = MagicMock(return_value=True)

    with patch("hft_platform.services._md_reconnect.timebase") as tb:
        tb.now_s.return_value = 150.0  # 50s wall clock
        with patch("hft_platform.services._md_reconnect.logger") as log:
            await md._run_monitor_reconnect_checks(gap=10.0)
            first_warn_calls = [c for c in log.warning.call_args_list if c.args and c.args[0] == "Heartbeat missing"]
            assert len(first_warn_calls) == 1

            # Second call 10s later (within 60s cooldown) → no new warning
            tb.now_s.return_value = 160.0
            await md._run_monitor_reconnect_checks(gap=10.0)
            all_warn_calls = [c for c in log.warning.call_args_list if c.args and c.args[0] == "Heartbeat missing"]
            assert len(all_warn_calls) == 1, "cooldown should suppress repeated warnings"


@pytest.mark.asyncio
async def test_heartbeat_warning_resumes_after_cooldown(md):
    """After cooldown elapses, next gap re-emits the warning."""
    md._attempt_resubscribe = AsyncMock()
    md._request_reconnect = AsyncMock()
    md._should_rollover_reconnect = MagicMock(return_value=False)
    md._is_trading_hours = MagicMock(return_value=True)

    with patch("hft_platform.services._md_reconnect.timebase") as tb:
        tb.now_s.return_value = 150.0
        with patch("hft_platform.services._md_reconnect.logger") as log:
            await md._run_monitor_reconnect_checks(gap=10.0)
            tb.now_s.return_value = 211.0  # 61s later
            await md._run_monitor_reconnect_checks(gap=10.0)
            warn_calls = [c for c in log.warning.call_args_list if c.args and c.args[0] == "Heartbeat missing"]
            assert len(warn_calls) == 2, "warning should resume after cooldown"


@pytest.mark.asyncio
async def test_heartbeat_warning_suppressed_outside_trading_hours(md):
    """Outside trading hours, the warning downgrades to a 5-min interval info."""
    md._attempt_resubscribe = AsyncMock()
    md._request_reconnect = AsyncMock()
    md._should_rollover_reconnect = MagicMock(return_value=False)
    md._is_trading_hours = MagicMock(return_value=False)

    with patch("hft_platform.services._md_reconnect.timebase") as tb:
        tb.now_s.return_value = 10000.0
        with patch("hft_platform.services._md_reconnect.logger") as log:
            await md._run_monitor_reconnect_checks(gap=10.0)
            warn_calls = [c for c in log.warning.call_args_list if c.args and c.args[0] == "Heartbeat missing"]
            info_calls = [
                c for c in log.info.call_args_list
                if c.args and c.args[0] == "Skipping heartbeat warning outside trading hours"
            ]
            assert len(warn_calls) == 0, "warning must not fire outside trading hours"
            assert len(info_calls) == 1, "off-hours info should fire once"
