"""Smoke tests for DataCollector._resolve_alias — TXFR1→TXFE6 (Bug P4).

Background
----------
``hft.market_data`` stores resolved month codes (e.g. ``TXFE6``); the daily
report bot defaults to the alias form ``TXFR1`` (configured by
``HFT_REPORT_SYMBOLS``). Without alias resolution the SQL ``WHERE symbol =
'TXFR1'`` returns 0 rows and the Telegram report shows zeros.

These tests pin the alias-resolution contract:
  1. Alias forms (R1/R2/C0/C1) are rewritten to whichever month code has the
     highest recent volume in CH.
  2. Already-resolved month codes (TXFE6) pass through unchanged.
  3. Stocks (2330) and options (TXO202604027000C) pass through unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.reports.collector import DataCollector


def _make_collector() -> tuple[DataCollector, MagicMock]:
    """Return a DataCollector with a mocked ``_execute`` callable."""
    with patch("hft_platform.reports.collector._make_execute") as mock_factory:
        mock_execute = MagicMock(return_value=[])
        mock_factory.return_value = mock_execute
        collector = DataCollector(ch_host="mock-host")
    collector._execute = mock_execute
    return collector, mock_execute


class TestAliasResolution:
    def test_query_ohlcv_resolves_txfr1_to_active_month(self) -> None:
        """``_query_ohlcv("TXFR1", ...)`` must query CH for the active month code."""
        collector, mock_execute = _make_collector()
        # First call = alias resolution, second call = the OHLCV query itself.
        mock_execute.side_effect = [
            [("TXFE6", 3_031_352)],  # alias-resolution result
            [],  # OHLCV result (irrelevant — we only check the param)
        ]

        collector._query_ohlcv("TXFR1", "exch_ts > 0")

        assert mock_execute.call_count == 2
        # The OHLCV call (second) must carry the resolved month code.
        ohlcv_params = mock_execute.call_args_list[1].args[1]
        assert ohlcv_params == {"symbol": "TXFE6"}

    def test_query_ohlcv_idempotent_for_resolved_month_code(self) -> None:
        """``_query_ohlcv("TXFE6", ...)`` must NOT trigger alias resolution."""
        collector, mock_execute = _make_collector()
        mock_execute.return_value = []

        collector._query_ohlcv("TXFE6", "exch_ts > 0")

        # Exactly one call — the OHLCV query itself, no alias-resolution probe.
        assert mock_execute.call_count == 1
        ohlcv_params = mock_execute.call_args.args[1]
        assert ohlcv_params == {"symbol": "TXFE6"}

    def test_stocks_pass_through_unchanged(self) -> None:
        """Stock symbol ``2330`` must not be alias-resolved."""
        collector, mock_execute = _make_collector()
        mock_execute.return_value = []

        collector._query_ohlcv("2330", "exch_ts > 0")

        assert mock_execute.call_count == 1
        assert mock_execute.call_args.args[1] == {"symbol": "2330"}

    def test_options_pass_through_unchanged(self) -> None:
        """Option contract symbols must not match the future-alias regex."""
        collector, mock_execute = _make_collector()
        mock_execute.return_value = []

        collector._query_ohlcv("TXO202604027000C", "exch_ts > 0")

        assert mock_execute.call_count == 1
        assert mock_execute.call_args.args[1] == {"symbol": "TXO202604027000C"}

    def test_alias_resolution_cached(self) -> None:
        """Repeat alias lookups within one collector reuse the first result."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            [("TXFE6", 3_031_352)],  # alias-resolution probe
            [],  # first OHLCV
            [],  # second OHLCV — must NOT trigger another alias probe
        ]

        collector._query_ohlcv("TXFR1", "exch_ts > 0")
        collector._query_ohlcv("TXFR1", "exch_ts > 0")

        # 1 alias-resolve + 2 OHLCV = 3 total. Alias probe ran once.
        assert mock_execute.call_count == 3

    def test_alias_resolution_falls_back_when_no_match(self) -> None:
        """If CH has no active month for the root, return alias unchanged."""
        collector, mock_execute = _make_collector()
        mock_execute.side_effect = [
            [],  # alias-resolution probe finds nothing
            [],  # OHLCV
        ]

        collector._query_ohlcv("TXFR1", "exch_ts > 0")

        ohlcv_params = mock_execute.call_args_list[1].args[1]
        # Falls back to the alias rather than crashing — caller will see zero
        # rows but pipeline does not blow up.
        assert ohlcv_params == {"symbol": "TXFR1"}
