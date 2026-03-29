"""Tests for build_report() extracted from pipeline.py.

Covers:
- test_returns_rendered_dict_on_success: all 4 stages mocked, returns dict with free/paid keys
- test_returns_none_when_no_data: collector returns tick_count=0, returns None
- test_default_symbol_is_txfd6: collector.collect called with "TXFD6"
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mock_session_data():
    sd = MagicMock()
    sd.tick_count = 100
    sd.bars_5m = [MagicMock()] * 12
    return sd


@pytest.fixture()
def mock_signal_report():
    sr = MagicMock()
    sr.bias = "bullish"
    sr.bias_confidence = 0.75
    return sr


@pytest.fixture()
def mock_scenario_report():
    scr = MagicMock()
    scr.direction = "long"
    scr.scenarios = [MagicMock(), MagicMock()]
    return scr


_PATCH_COLLECTOR = "hft_platform.reports.collector.DataCollector"
_PATCH_SIGNAL = "hft_platform.reports.signals.SignalEngine"
_PATCH_SCENARIO = "hft_platform.reports.scenarios.ScenarioBuilder"
_PATCH_RENDERER = "hft_platform.reports.renderer.ReportRenderer"


def _make_stage_mocks(session_data, signal_report, scenario_report):
    """Return mock instances for all 4 pipeline stages."""
    mock_collector = MagicMock()
    mock_collector.collect.return_value = session_data

    mock_engine = MagicMock()
    mock_engine.analyze.return_value = signal_report

    mock_builder = MagicMock()
    mock_builder.build.return_value = scenario_report

    mock_renderer = MagicMock()
    mock_renderer.render.side_effect = lambda _scr, tier: [f"{tier}_msg_1", f"{tier}_msg_2"]

    return mock_collector, mock_engine, mock_builder, mock_renderer


class TestBuildReport:
    def test_returns_rendered_dict_on_success(
        self, mock_session_data, mock_signal_report, mock_scenario_report
    ):
        """build_report returns dict with 'free' and 'paid' keys when data exists."""
        collector, engine, builder, renderer = _make_stage_mocks(
            mock_session_data, mock_signal_report, mock_scenario_report
        )

        with (
            patch(_PATCH_COLLECTOR, return_value=collector),
            patch(_PATCH_SIGNAL, return_value=engine),
            patch(_PATCH_SCENARIO, return_value=builder),
            patch(_PATCH_RENDERER, return_value=renderer),
        ):
            from hft_platform.reports.pipeline import build_report

            result = build_report("day", "2026-03-28")

        assert result is not None
        assert "free" in result
        assert "paid" in result
        assert result["free"] == ["free_msg_1", "free_msg_2"]
        assert result["paid"] == ["paid_msg_1", "paid_msg_2"]

    def test_returns_none_when_no_data(self, mock_signal_report, mock_scenario_report):
        """build_report returns None when session has zero ticks."""
        empty_session_data = MagicMock()
        empty_session_data.tick_count = 0
        empty_session_data.bars_5m = []

        collector, engine, builder, renderer = _make_stage_mocks(
            empty_session_data, mock_signal_report, mock_scenario_report
        )

        with (
            patch(_PATCH_COLLECTOR, return_value=collector),
            patch(_PATCH_SIGNAL, return_value=engine),
            patch(_PATCH_SCENARIO, return_value=builder),
            patch(_PATCH_RENDERER, return_value=renderer),
        ):
            from hft_platform.reports.pipeline import build_report

            result = build_report("night", "2026-03-27")

        assert result is None
        # Stages 2-4 should never be called when there's no data
        engine.analyze.assert_not_called()
        builder.build.assert_not_called()
        renderer.render.assert_not_called()

    def test_default_symbol_is_txfd6(
        self, mock_session_data, mock_signal_report, mock_scenario_report
    ):
        """build_report passes 'TXFD6' as the default symbol to collector.collect."""
        collector, engine, builder, renderer = _make_stage_mocks(
            mock_session_data, mock_signal_report, mock_scenario_report
        )

        with (
            patch(_PATCH_COLLECTOR, return_value=collector),
            patch(_PATCH_SIGNAL, return_value=engine),
            patch(_PATCH_SCENARIO, return_value=builder),
            patch(_PATCH_RENDERER, return_value=renderer),
        ):
            from hft_platform.reports.pipeline import build_report

            build_report("day", "2026-03-28")

        collector.collect.assert_called_once_with("day", "2026-03-28", "TXFD6")

    def test_custom_symbol_is_forwarded(
        self, mock_session_data, mock_signal_report, mock_scenario_report
    ):
        """build_report forwards a custom symbol to collector.collect."""
        collector, engine, builder, renderer = _make_stage_mocks(
            mock_session_data, mock_signal_report, mock_scenario_report
        )

        with (
            patch(_PATCH_COLLECTOR, return_value=collector),
            patch(_PATCH_SIGNAL, return_value=engine),
            patch(_PATCH_SCENARIO, return_value=builder),
            patch(_PATCH_RENDERER, return_value=renderer),
        ):
            from hft_platform.reports.pipeline import build_report

            build_report("day", "2026-03-28", symbol="TMFD6")

        collector.collect.assert_called_once_with("day", "2026-03-28", "TMFD6")

    def test_build_report_in_all_exports(self):
        """build_report is listed in __all__."""
        import hft_platform.reports.pipeline as mod

        assert "build_report" in mod.__all__
