"""Tests for DataCollector.collect_core() — lightweight Q1-Q4 only path."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.reports.collector import DataCollector
from hft_platform.reports.models import SessionData

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_collector() -> tuple[DataCollector, MagicMock]:
    """Return a DataCollector with a mocked _execute function."""
    with patch("hft_platform.reports.collector._make_execute") as mock_factory:
        mock_execute = MagicMock(return_value=[])
        mock_factory.return_value = mock_execute
        collector = DataCollector(ch_host="mock-host")
    collector._execute = mock_execute
    return collector, mock_execute


def _make_ohlcv_row() -> list[tuple]:
    """Single OHLCV row in ClickHouse units (x1,000,000).

    Column order matches _query_ohlcv SQL:
      (open_ch, close_ch, low_ch, high_ch, volume, tick_count)
    """
    return [(2_000_000_000, 2_050_000_000, 1_900_000_000, 2_100_000_000, 1000, 500)]


def _make_bar_row() -> list[tuple]:
    return [("2026-03-28 09:00:00", 2_000_000_000, 2_100_000_000, 1_900_000_000, 2_050_000_000, 200, 100)]


def _make_flow_row() -> list[tuple]:
    return [("2026-03-28 09:00:00", 100, 500, 300, 150, 50)]


def _make_large_trade_row() -> list[tuple]:
    return [("2026-03-28 09:01:00", 2_000_000_000, 20, 1_990_000_000)]


# ---------------------------------------------------------------------------
# collect_core() — unit tests
# ---------------------------------------------------------------------------


class TestCollectCore:
    def test_returns_session_data(self) -> None:
        """collect_core() returns a SessionData instance."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),  # Q1
            _make_bar_row(),  # Q2
            _make_flow_row(),  # Q3
            _make_large_trade_row(),  # Q4
        ]
        result = collector.collect_core("TXFD6", "exch_ts > 0")
        assert isinstance(result, SessionData)

    def test_spread_dist_is_empty(self) -> None:
        """collect_core() always returns empty spread_dist (no Q5)."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
        ]
        result = collector.collect_core("TXFD6", "exch_ts > 0")
        assert result.spread_dist == {}

    def test_depth_imbalance_is_empty(self) -> None:
        """collect_core() always returns empty depth_imbalance (no Q6)."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
        ]
        result = collector.collect_core("TXFD6", "exch_ts > 0")
        assert result.depth_imbalance == []

    def test_executes_exactly_4_queries(self) -> None:
        """collect_core() calls _execute exactly 4 times (Q1–Q4 only)."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
        ]
        collector.collect_core("TXFD6", "exch_ts > 0")
        assert mock_execute.call_count == 4

    def test_symbol_propagated(self) -> None:
        """collect_core() stores symbol on the returned SessionData."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
        ]
        result = collector.collect_core("TMFD6", "exch_ts > 0", session="day", date="2026-03-28")
        assert result.symbol == "TMFD6"

    def test_session_and_date_propagated(self) -> None:
        """collect_core() passes through session and date kwargs."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
        ]
        result = collector.collect_core("TXFD6", "exch_ts > 0", session="night", date="2026-03-27")
        assert result.session == "night"
        assert result.date == "2026-03-27"

    def test_ohlcv_prices_converted(self) -> None:
        """collect_core() converts CH prices to platform scale (÷100)."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            [],  # Q2 — empty bars
            [],  # Q3
            [],  # Q4
        ]
        result = collector.collect_core("TXFD6", "exch_ts > 0")
        # Row order: (open_ch, close_ch, low_ch, high_ch, volume, tick_count)
        # CH open = 2_000_000_000 → platform = 2_000_000_000 // 100 = 20_000_000
        assert result.open == 20_000_000
        assert result.close == 20_500_000
        assert result.low == 19_000_000
        assert result.high == 21_000_000

    def test_empty_ohlcv_returns_zeros(self) -> None:
        """collect_core() handles missing OHLCV data gracefully."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            [],  # Q1 — no data
            [],  # Q2
            [],  # Q3
            [],  # Q4
        ]
        result = collector.collect_core("TXFD6", "exch_ts > 0")
        assert result.open == 0
        assert result.volume == 0
        assert result.tick_count == 0


# ---------------------------------------------------------------------------
# collect() — delegation tests
# ---------------------------------------------------------------------------


class TestCollectDelegation:
    def test_collect_executes_6_queries(self) -> None:
        """collect() runs Q1-Q6, i.e. exactly 6 _execute calls."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),  # Q1
            _make_bar_row(),  # Q2
            _make_flow_row(),  # Q3
            _make_large_trade_row(),  # Q4
            [(1, 500)],  # Q5 spread
            [(9, 10.0, 8.0)],  # Q6 depth
        ]
        result = collector.collect("day", "2026-03-28", "TXFD6")
        assert mock_execute.call_count == 6

    def test_collect_returns_session_data(self) -> None:
        """collect() returns SessionData."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
            [(1, 500)],
            [(9, 10.0, 8.0)],
        ]
        result = collector.collect("day", "2026-03-28", "TXFD6")
        assert isinstance(result, SessionData)

    def test_collect_spread_populated(self) -> None:
        """collect() fills spread_dist from Q5."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
            [(1, 500), (2, 300)],
            [],
        ]
        result = collector.collect("day", "2026-03-28", "TXFD6")
        assert result.spread_dist == {1: 500, 2: 300}

    def test_collect_depth_populated(self) -> None:
        """collect() fills depth_imbalance from Q6."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
            [],
            [(9, 10.0, 8.0)],
        ]
        result = collector.collect("day", "2026-03-28", "TXFD6")
        assert len(result.depth_imbalance) == 1
        assert result.depth_imbalance[0].hour == 9

    def test_collect_q5_failure_degrades_gracefully(self) -> None:
        """collect() sets spread_dist={} when Q5 raises."""
        collector, mock_execute = _make_collector()

        def _side_effect(sql: str) -> list:
            if "asks_price" in sql:
                raise MemoryError("OOM")
            if "bids_vol" in sql:
                return []
            if "lagInFrame" in sql and "volume >=" in sql:
                return _make_large_trade_row()
            if "lagInFrame" in sql:
                return _make_flow_row()
            if "toStartOfFiveMinutes" in sql:
                return _make_bar_row()
            return _make_ohlcv_row()

        mock_execute.side_effect = _side_effect
        result = collector.collect("day", "2026-03-28", "TXFD6")
        assert result.spread_dist == {}

    def test_collect_q6_failure_degrades_gracefully(self) -> None:
        """collect() sets depth_imbalance=[] when Q6 raises."""
        collector, mock_execute = _make_collector()

        def _side_effect(sql: str) -> list:
            if "bids_vol" in sql:
                raise MemoryError("OOM")
            if "asks_price" in sql:
                return [(1, 500)]
            if "lagInFrame" in sql and "volume >=" in sql:
                return _make_large_trade_row()
            if "lagInFrame" in sql:
                return _make_flow_row()
            if "toStartOfFiveMinutes" in sql:
                return _make_bar_row()
            return _make_ohlcv_row()

        mock_execute.side_effect = _side_effect
        result = collector.collect("day", "2026-03-28", "TXFD6")
        assert result.depth_imbalance == []

    def test_collect_core_data_matches_collect(self) -> None:
        """core fields returned by collect() match collect_core() output."""
        collector, mock_execute = _make_collector()

        rows = [
            _make_ohlcv_row(),
            _make_bar_row(),
            _make_flow_row(),
            _make_large_trade_row(),
        ]

        # collect_core call
        mock_execute.side_effect = rows[:]
        core = collector.collect_core("TXFD6", "exch_ts > 0", session="day", date="2026-03-28")

        # collect call (same queries + Q5/Q6)
        mock_execute.side_effect = rows[:] + [[], []]
        full = collector.collect("day", "2026-03-28", "TXFD6")

        assert core.open == full.open
        assert core.high == full.high
        assert core.low == full.low
        assert core.close == full.close
        assert core.volume == full.volume
        assert core.tick_count == full.tick_count
        assert len(core.bars_5m) == len(full.bars_5m)
        assert len(core.flow_5m) == len(full.flow_5m)
        assert len(core.large_trades) == len(full.large_trades)
