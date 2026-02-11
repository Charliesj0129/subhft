"""Tests for WAL scheduler."""

import datetime as dt
import os
import time
from unittest.mock import MagicMock, patch

import pytest


def test_wal_scheduler_initialization():
    """Test WALScheduler initializes correctly."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)

    assert scheduler._loader is mock_loader
    assert scheduler._running is False
    assert scheduler._close_buffer_s == 300  # Default 5 minutes


def test_wal_scheduler_start_stop():
    """Test WALScheduler start and stop."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)
    scheduler._check_interval_s = 0.1  # Fast check for test

    scheduler.start()
    assert scheduler.running is True
    assert scheduler._thread is not None
    assert scheduler._thread.is_alive()

    scheduler.stop()
    assert scheduler.running is False


def test_wal_scheduler_disabled_by_env():
    """Test WALScheduler respects HFT_WAL_SCHEDULER_ENABLED=0."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()

    with patch.dict(os.environ, {"HFT_WAL_SCHEDULER_ENABLED": "0"}):
        scheduler = WALScheduler(mock_loader)
        scheduler.start()

        assert scheduler.running is False
        assert scheduler._thread is None


def test_wal_scheduler_trigger_flush():
    """Test manual trigger_flush method."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)

    result = scheduler.trigger_flush()

    assert result is True
    mock_loader.process_files.assert_called_once_with(force=True)


def test_wal_scheduler_trigger_flush_handles_error():
    """Test trigger_flush handles error gracefully with retries."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    mock_loader.process_files.side_effect = RuntimeError("boom")
    scheduler = WALScheduler(mock_loader)
    scheduler._flush_max_retries = 1  # No retries for faster test
    scheduler._flush_base_delay_s = 0.01

    # trigger_flush now returns False after all retries exhausted
    result = scheduler.trigger_flush()
    assert result is False  # Returns False because all retries failed
    mock_loader.process_files.assert_called_once_with(force=True)


def test_wal_scheduler_check_and_flush_non_trading_day():
    """Test _check_and_flush skips non-trading days."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)

    # Mock calendar to return non-trading day
    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = False
    mock_calendar._tz = dt.timezone.utc
    scheduler._calendar = mock_calendar

    scheduler._check_and_flush()

    mock_loader.process_files.assert_not_called()


def test_wal_scheduler_check_and_flush_before_close():
    """Test _check_and_flush does not flush before close time."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)
    scheduler._close_buffer_s = 300

    # Mock calendar
    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = True
    mock_calendar._tz = dt.timezone.utc
    # Close time is 13:30, current time is 13:00 - before close + buffer
    close_time = dt.datetime(2026, 2, 11, 13, 30, tzinfo=dt.timezone.utc)
    mock_calendar.get_session_close.return_value = close_time
    scheduler._calendar = mock_calendar

    # Freeze time to before trigger
    with patch("hft_platform.recorder.wal_scheduler.dt") as mock_dt:
        now = dt.datetime(2026, 2, 11, 13, 0, tzinfo=dt.timezone.utc)
        mock_dt.datetime.now.return_value = now
        mock_dt.timedelta = dt.timedelta

        scheduler._check_and_flush()

    mock_loader.process_files.assert_not_called()


def test_wal_scheduler_check_and_flush_after_close():
    """Test _check_and_flush flushes after close + buffer."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)
    scheduler._close_buffer_s = 300  # 5 minutes

    # Mock calendar
    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = True
    mock_calendar._tz = dt.timezone.utc
    close_time = dt.datetime(2026, 2, 11, 13, 30, tzinfo=dt.timezone.utc)
    mock_calendar.get_session_close.return_value = close_time
    scheduler._calendar = mock_calendar

    # Freeze time to after trigger (13:36 > 13:35)
    with patch("hft_platform.recorder.wal_scheduler.dt") as mock_dt:
        now = dt.datetime(2026, 2, 11, 13, 36, tzinfo=dt.timezone.utc)
        mock_dt.datetime.now.return_value = now
        mock_dt.timedelta = dt.timedelta

        scheduler._check_and_flush()

    mock_loader.process_files.assert_called_once_with(force=True)
    assert scheduler._last_flush_date == dt.date(2026, 2, 11)


def test_wal_scheduler_no_double_flush():
    """Test _check_and_flush does not flush twice on same day."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)
    scheduler._close_buffer_s = 300

    # Mock calendar
    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = True
    mock_calendar._tz = dt.timezone.utc
    close_time = dt.datetime(2026, 2, 11, 13, 30, tzinfo=dt.timezone.utc)
    mock_calendar.get_session_close.return_value = close_time
    scheduler._calendar = mock_calendar

    # Set already flushed today
    scheduler._last_flush_date = dt.date(2026, 2, 11)

    with patch("hft_platform.recorder.wal_scheduler.dt") as mock_dt:
        now = dt.datetime(2026, 2, 11, 14, 0, tzinfo=dt.timezone.utc)
        mock_dt.datetime.now.return_value = now
        mock_dt.timedelta = dt.timedelta

        scheduler._check_and_flush()

    mock_loader.process_files.assert_not_called()


def test_wal_scheduler_metrics_on_success():
    """Test WALScheduler emits metrics on successful flush."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)

    mock_metrics = MagicMock()
    scheduler._metrics = mock_metrics

    scheduler._do_batch_flush()

    mock_metrics.wal_batch_flush_total.labels.assert_called_with(result="ok")
    mock_metrics.wal_batch_flush_total.labels().inc.assert_called_once()


def test_wal_scheduler_metrics_on_error():
    """Test WALScheduler emits metrics on failed flush."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    mock_loader.process_files.side_effect = RuntimeError("boom")
    scheduler = WALScheduler(mock_loader)
    scheduler._flush_max_retries = 1  # No retries for this test

    mock_metrics = MagicMock()
    scheduler._metrics = mock_metrics

    scheduler._do_batch_flush()

    mock_metrics.wal_batch_flush_total.labels.assert_called_with(result="error")
    mock_metrics.wal_batch_flush_total.labels().inc.assert_called_once()


def test_batch_flush_retry_on_failure():
    """Test WAL batch flush retries with exponential backoff."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    # Fail first two times, succeed third
    mock_loader.process_files.side_effect = [
        RuntimeError("fail 1"),
        RuntimeError("fail 2"),
        None,  # Success
    ]

    scheduler = WALScheduler(mock_loader)
    scheduler._flush_max_retries = 3
    scheduler._flush_base_delay_s = 0.01  # Fast for test
    scheduler._flush_max_delay_s = 1.0

    mock_metrics = MagicMock()
    scheduler._metrics = mock_metrics

    result = scheduler._do_batch_flush()

    assert result is True
    assert mock_loader.process_files.call_count == 3
    # Two retries should have been recorded
    assert mock_metrics.wal_batch_flush_retry_total.inc.call_count == 2
    mock_metrics.wal_batch_flush_total.labels.assert_called_with(result="ok")


def test_batch_flush_retry_exhausted():
    """Test WAL batch flush fails after max retries."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    mock_loader.process_files.side_effect = RuntimeError("always fail")

    scheduler = WALScheduler(mock_loader)
    scheduler._flush_max_retries = 3
    scheduler._flush_base_delay_s = 0.01  # Fast for test
    scheduler._flush_max_delay_s = 1.0

    mock_metrics = MagicMock()
    scheduler._metrics = mock_metrics

    result = scheduler._do_batch_flush()

    assert result is False
    assert mock_loader.process_files.call_count == 3
    # Two retries (not counting first attempt)
    assert mock_metrics.wal_batch_flush_retry_total.inc.call_count == 2
    mock_metrics.wal_batch_flush_total.labels.assert_called_with(result="error")


def test_adaptive_check_interval_non_trading_day():
    """Test adaptive interval returns 1 hour for non-trading days."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)

    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = False
    mock_calendar._tz = dt.timezone.utc
    scheduler._calendar = mock_calendar

    with patch("hft_platform.recorder.wal_scheduler.dt") as mock_dt:
        mock_dt.datetime.now.return_value = dt.datetime(2026, 2, 14, 10, 0, tzinfo=dt.timezone.utc)
        mock_dt.timedelta = dt.timedelta

        interval = scheduler._get_check_interval()

    assert interval == 3600.0


def test_adaptive_check_interval_trading_hours():
    """Test adaptive interval returns 1 minute during trading hours."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)

    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = True
    mock_calendar._tz = dt.timezone.utc
    open_time = dt.datetime(2026, 2, 16, 9, 0, tzinfo=dt.timezone.utc)
    close_time = dt.datetime(2026, 2, 16, 13, 30, tzinfo=dt.timezone.utc)
    mock_calendar.get_session_open.return_value = open_time
    mock_calendar.get_session_close.return_value = close_time
    scheduler._calendar = mock_calendar

    # During trading hours (10:00)
    with patch("hft_platform.recorder.wal_scheduler.dt") as mock_dt:
        mock_dt.datetime.now.return_value = dt.datetime(2026, 2, 16, 10, 0, tzinfo=dt.timezone.utc)
        mock_dt.timedelta = dt.timedelta

        interval = scheduler._get_check_interval()

    assert interval == 60.0


def test_adaptive_check_interval_pre_market():
    """Test adaptive interval returns 5 minutes during pre-market."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)

    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = True
    mock_calendar._tz = dt.timezone.utc
    open_time = dt.datetime(2026, 2, 16, 9, 0, tzinfo=dt.timezone.utc)
    close_time = dt.datetime(2026, 2, 16, 13, 30, tzinfo=dt.timezone.utc)
    mock_calendar.get_session_open.return_value = open_time
    mock_calendar.get_session_close.return_value = close_time
    scheduler._calendar = mock_calendar

    # Pre-market (8:30 - within 1 hour before open)
    with patch("hft_platform.recorder.wal_scheduler.dt") as mock_dt:
        mock_dt.datetime.now.return_value = dt.datetime(2026, 2, 16, 8, 30, tzinfo=dt.timezone.utc)
        mock_dt.timedelta = dt.timedelta

        interval = scheduler._get_check_interval()

    assert interval == 300.0


def test_adaptive_check_interval_post_close_buffer():
    """Test adaptive interval returns 1 minute during post-close buffer."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)
    scheduler._close_buffer_s = 300  # 5 min buffer

    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = True
    mock_calendar._tz = dt.timezone.utc
    open_time = dt.datetime(2026, 2, 16, 9, 0, tzinfo=dt.timezone.utc)
    close_time = dt.datetime(2026, 2, 16, 13, 30, tzinfo=dt.timezone.utc)
    mock_calendar.get_session_open.return_value = open_time
    mock_calendar.get_session_close.return_value = close_time
    scheduler._calendar = mock_calendar

    # Post-close buffer (13:35 - 5 minutes after close)
    with patch("hft_platform.recorder.wal_scheduler.dt") as mock_dt:
        mock_dt.datetime.now.return_value = dt.datetime(2026, 2, 16, 13, 35, tzinfo=dt.timezone.utc)
        mock_dt.timedelta = dt.timedelta

        interval = scheduler._get_check_interval()

    assert interval == 60.0


def test_adaptive_check_interval_after_buffer():
    """Test adaptive interval returns 1 hour after post-close buffer."""
    from hft_platform.recorder.wal_scheduler import WALScheduler

    mock_loader = MagicMock()
    scheduler = WALScheduler(mock_loader)
    scheduler._close_buffer_s = 300  # 5 min buffer

    mock_calendar = MagicMock()
    mock_calendar.is_trading_day.return_value = True
    mock_calendar._tz = dt.timezone.utc
    open_time = dt.datetime(2026, 2, 16, 9, 0, tzinfo=dt.timezone.utc)
    close_time = dt.datetime(2026, 2, 16, 13, 30, tzinfo=dt.timezone.utc)
    mock_calendar.get_session_open.return_value = open_time
    mock_calendar.get_session_close.return_value = close_time
    scheduler._calendar = mock_calendar

    # Well after buffer (15:00)
    with patch("hft_platform.recorder.wal_scheduler.dt") as mock_dt:
        mock_dt.datetime.now.return_value = dt.datetime(2026, 2, 16, 15, 0, tzinfo=dt.timezone.utc)
        mock_dt.timedelta = dt.timedelta

        interval = scheduler._get_check_interval()

    assert interval == 3600.0
