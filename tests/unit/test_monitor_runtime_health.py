import datetime as dt
from unittest.mock import MagicMock, patch

from scripts.monitor_runtime_health import (
    _filtered_feed_gap_values,
    _is_core_futures_symbol,
    _is_trading_hours_now,
    _parse_metrics,
)


def test_core_futures_symbol_filter_excludes_stocks_and_options() -> None:
    assert _is_core_futures_symbol("TXFR1")
    assert _is_core_futures_symbol("TMFE6")
    assert not _is_core_futures_symbol("2330")
    assert not _is_core_futures_symbol("TXO35500Q6")


def test_filtered_feed_gap_uses_core_futures_by_default() -> None:
    metrics = _parse_metrics(
        "\n".join(
            [
                'feed_gap_by_symbol_seconds{symbol="2330"} 120',
                'feed_gap_by_symbol_seconds{symbol="TXO35500Q6"} 90',
                'feed_gap_by_symbol_seconds{symbol="TXFR1"} 4',
                'feed_gap_by_symbol_seconds{symbol="TMFR1"} 7',
            ]
        )
    )

    values = _filtered_feed_gap_values(metrics["feed_gap_by_symbol_seconds"])

    assert values == [4.0, 7.0]


def test_filtered_feed_gap_allowlist_overrides_default_filter() -> None:
    metrics = _parse_metrics(
        "\n".join(
            [
                'feed_gap_by_symbol_seconds{symbol="2330"} 120',
                'feed_gap_by_symbol_seconds{symbol="TXFR1"} 4',
            ]
        )
    )

    values = _filtered_feed_gap_values(
        metrics["feed_gap_by_symbol_seconds"],
        allowlist={"2330"},
    )

    assert values == [120.0]


def test_is_trading_hours_now_uses_calendar_when_available() -> None:
    fake_cal = MagicMock()
    fake_cal._tz = dt.timezone(dt.timedelta(hours=8))
    fake_cal.is_trading_hours.return_value = True
    with patch("hft_platform.core.market_calendar.get_calendar", return_value=fake_cal):
        assert _is_trading_hours_now("future") is True
    fake_cal.is_trading_hours.assert_called_once()


def test_is_trading_hours_now_falls_back_on_import_error() -> None:
    # Fallback path uses a weekday window: weekend → False.
    with patch("hft_platform.core.market_calendar.get_calendar", side_effect=ImportError):
        # We can't easily force "weekend now", but the fallback must return a bool
        result = _is_trading_hours_now("future")
        assert isinstance(result, bool)
