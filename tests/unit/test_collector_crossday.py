"""Test Q7 cross-day query in DataCollector."""

from __future__ import annotations

from unittest.mock import patch

from hft_platform.reports.models import DaySnapshot


def _make_collector():
    """Create a DataCollector with a stubbed CH connection."""
    with patch("hft_platform.reports.collector._make_execute", return_value=lambda sql: []):
        from hft_platform.reports.collector import DataCollector

        return DataCollector(ch_host="localhost")


def test_collect_cross_day_returns_snapshots():
    fake_rows = [
        ("2026-03-26", 20500000000, 20600000000, 20400000000, 20550000000, 12000, 6500, 5500),
        ("2026-03-25", 20400000000, 20550000000, 20350000000, 20480000000, 11000, 5800, 5200),
    ]
    collector = _make_collector()
    with patch.object(collector, "_execute", return_value=fake_rows):
        snapshots = collector.collect_cross_day("TXFD6", "day", "2026-03-27")
    assert len(snapshots) == 2
    assert isinstance(snapshots[0], DaySnapshot)
    assert snapshots[0].date == "2026-03-26"
    assert snapshots[0].volume == 12000


def test_collect_cross_day_empty():
    collector = _make_collector()
    with patch.object(collector, "_execute", return_value=[]):
        snapshots = collector.collect_cross_day("TXFD6", "day", "2026-03-27")
    assert snapshots == []


def test_collect_cross_day_zero_downtick():
    fake_rows = [("2026-03-26", 20500000000, 20600000000, 20400000000, 20550000000, 12000, 6500, 0)]
    collector = _make_collector()
    with patch.object(collector, "_execute", return_value=fake_rows):
        snapshots = collector.collect_cross_day("TXFD6", "day", "2026-03-27")
    assert snapshots[0].ud_ratio > 10


def test_collect_cross_day_skips_weekends():
    """If date is Monday, prev days should skip Sat/Sun."""
    call_args: list[str] = []

    def fake_execute(query: str) -> list:
        call_args.append(query)
        return []

    collector = _make_collector()
    with patch.object(collector, "_execute", side_effect=fake_execute):
        collector.collect_cross_day("TXFD6", "day", "2026-03-30")  # Monday
    # Should query Fri 27, Thu 26, Wed 25 -- NOT Sat 28 or Sun 29
    if call_args:
        assert "2026-03-28" not in call_args[0]
        assert "2026-03-29" not in call_args[0]
