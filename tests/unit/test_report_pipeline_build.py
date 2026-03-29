"""Tests for build_report() extracted from pipeline.py.

Covers:
- test_returns_composed_report_on_success: all 4 stages mocked, returns ComposedReport
- test_returns_none_when_no_data: collector returns tick_count=0, returns None
- test_default_symbol_is_txfd6: collector.collect called with "TXFD6"
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.reports.models import ComposedReport, MessagePart


@pytest.fixture()
def mock_session_data():
    sd = MagicMock()
    sd.tick_count = 100
    sd.bars_5m = [MagicMock()] * 12
    return sd


@pytest.fixture()
def mock_fact_report():
    fr = MagicMock()
    fr.segments = [MagicMock(), MagicMock()]
    return fr


@pytest.fixture()
def mock_reasoning_report():
    rr = MagicMock()
    rr.bias.bias = "bullish"
    rr.bias.confidence = 0.75
    rr.levels = [MagicMock()]
    return rr


_PATCH_COLLECTOR = "hft_platform.reports.collector.DataCollector"
_PATCH_EXTRACT = "hft_platform.reports.facts.extract_all"
_PATCH_REASON = "hft_platform.reports.reasoner.reason_all"
_PATCH_COMPOSER = "hft_platform.reports.composer.ReportComposer"


def _make_composed() -> ComposedReport:
    return ComposedReport(
        messages=[
            MessagePart(kind="text", content="free msg", min_tier="free"),
            MessagePart(kind="text", content="paid msg", min_tier="paid"),
        ]
    )


class TestBuildReport:
    def test_returns_composed_report_on_success(self, mock_session_data, mock_fact_report, mock_reasoning_report):
        """build_report returns ComposedReport when data exists."""
        mock_collector = MagicMock()
        mock_collector.collect.return_value = mock_session_data
        mock_collector.collect_cross_day.return_value = []

        composed = _make_composed()
        mock_composer_inst = MagicMock()
        mock_composer_inst.compose.return_value = composed

        with (
            patch(_PATCH_COLLECTOR, return_value=mock_collector),
            patch(_PATCH_EXTRACT, return_value=mock_fact_report),
            patch(_PATCH_REASON, return_value=mock_reasoning_report),
            patch(_PATCH_COMPOSER, return_value=mock_composer_inst),
        ):
            from hft_platform.reports.pipeline import build_report

            result = build_report("day", "2026-03-28")

        assert result is not None
        assert isinstance(result, ComposedReport)
        assert len(result.messages) == 2
        assert result.messages[0].content == "free msg"
        assert result.messages[1].content == "paid msg"

    def test_returns_none_when_no_data(self, mock_fact_report, mock_reasoning_report):
        """build_report returns None when session has zero ticks."""
        empty_session_data = MagicMock()
        empty_session_data.tick_count = 0
        empty_session_data.bars_5m = []

        mock_collector = MagicMock()
        mock_collector.collect.return_value = empty_session_data

        with (
            patch(_PATCH_COLLECTOR, return_value=mock_collector),
            patch(_PATCH_EXTRACT) as mock_extract,
            patch(_PATCH_REASON) as mock_reason,
            patch(_PATCH_COMPOSER) as mock_composer,
        ):
            from hft_platform.reports.pipeline import build_report

            result = build_report("night", "2026-03-27")

        assert result is None
        mock_extract.assert_not_called()
        mock_reason.assert_not_called()
        mock_composer.assert_not_called()

    def test_default_symbol_is_txfd6(self, mock_session_data, mock_fact_report, mock_reasoning_report):
        """build_report passes 'TXFD6' as the default symbol to collector.collect."""
        mock_collector = MagicMock()
        mock_collector.collect.return_value = mock_session_data
        mock_collector.collect_cross_day.return_value = []

        mock_composer_inst = MagicMock()
        mock_composer_inst.compose.return_value = _make_composed()

        with (
            patch(_PATCH_COLLECTOR, return_value=mock_collector),
            patch(_PATCH_EXTRACT, return_value=mock_fact_report),
            patch(_PATCH_REASON, return_value=mock_reasoning_report),
            patch(_PATCH_COMPOSER, return_value=mock_composer_inst),
        ):
            from hft_platform.reports.pipeline import build_report

            build_report("day", "2026-03-28")

        mock_collector.collect.assert_called_once_with("day", "2026-03-28", "TXFD6")

    def test_custom_symbol_is_forwarded(self, mock_session_data, mock_fact_report, mock_reasoning_report):
        """build_report forwards a custom symbol to collector.collect."""
        mock_collector = MagicMock()
        mock_collector.collect.return_value = mock_session_data
        mock_collector.collect_cross_day.return_value = []

        mock_composer_inst = MagicMock()
        mock_composer_inst.compose.return_value = _make_composed()

        with (
            patch(_PATCH_COLLECTOR, return_value=mock_collector),
            patch(_PATCH_EXTRACT, return_value=mock_fact_report),
            patch(_PATCH_REASON, return_value=mock_reasoning_report),
            patch(_PATCH_COMPOSER, return_value=mock_composer_inst),
        ):
            from hft_platform.reports.pipeline import build_report

            build_report("day", "2026-03-28", symbol="TMFD6")

        mock_collector.collect.assert_called_once_with("day", "2026-03-28", "TMFD6")

    def test_build_report_in_all_exports(self):
        """build_report is listed in __all__."""
        import hft_platform.reports.pipeline as mod

        assert "build_report" in mod.__all__
