"""Tests for build_report() extracted from pipeline.py.

Covers:
- test_returns_composed_report_on_success: all 4 stages mocked, returns ComposedReport
- test_returns_none_when_no_data: collector returns tick_count=0, returns None
- test_default_symbol_is_txfd6: collector.collect called with "TXFD6"
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
_PATCH_TO_THREAD = "hft_platform.reports.pipeline.asyncio.to_thread"


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


class TestBuildHybridReport:
    @pytest.mark.asyncio
    async def test_async_wrapper_runs_build_report_in_thread_and_adds_llm_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_fact_report,
        mock_reasoning_report,
    ):
        """Hybrid wrapper offloads sync stages and re-composes with the LLM decision."""
        monkeypatch.setenv("HFT_LLM_ENABLED", "1")
        composed = _make_composed()
        fake_dossier = MagicMock()
        fake_llm_report = MagicMock()

        mock_composer_inst = MagicMock()

        def compose(fr, rr, llm_decision=None):
            assert fr is mock_fact_report
            assert rr is mock_reasoning_report
            assert llm_decision is fake_llm_report
            return composed

        mock_composer_inst.compose.side_effect = compose

        with (
            patch(
                _PATCH_TO_THREAD, new=AsyncMock(return_value=(mock_fact_report, mock_reasoning_report, composed))
            ) as mock_to_thread,
            patch(_PATCH_COMPOSER, return_value=mock_composer_inst),
            patch("hft_platform.reports.pipeline.build_llm_dossier", return_value=fake_dossier) as mock_dossier,
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient") as MockClient,
        ):
            MockReasoner.return_value.generate = AsyncMock(return_value=fake_llm_report)
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is composed
        assert result.dossier is fake_dossier
        assert result.decision is fake_llm_report
        assert result.llm_error is None
        mock_to_thread.assert_awaited_once()
        assert mock_to_thread.await_args.args[1:] == ("day", "2026-03-28", "TXFD6")
        mock_dossier.assert_called_once_with(mock_fact_report, mock_reasoning_report)
        MockClient.assert_called_once()
        MockReasoner.return_value.generate.assert_awaited_once_with(fake_dossier)
        mock_composer_inst.compose.assert_called_once_with(
            mock_fact_report,
            mock_reasoning_report,
            llm_decision=fake_llm_report,
        )

    @pytest.mark.asyncio
    async def test_async_wrapper_falls_back_when_llm_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_fact_report,
        mock_reasoning_report,
    ):
        """Hybrid wrapper keeps deterministic composed output and surfaces llm_error."""
        monkeypatch.setenv("HFT_LLM_ENABLED", "1")
        composed = _make_composed()
        fake_dossier = MagicMock()

        with (
            patch(_PATCH_TO_THREAD, new=AsyncMock(return_value=(mock_fact_report, mock_reasoning_report, composed))),
            patch("hft_platform.reports.pipeline.build_llm_dossier", return_value=fake_dossier),
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient"),
            patch(_PATCH_COMPOSER) as mock_composer,
        ):
            MockReasoner.return_value.generate = AsyncMock(side_effect=RuntimeError("bad llm"))
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is composed
        assert result.dossier is None
        assert result.decision is None
        assert result.llm_error == "bad llm"
        mock_composer.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_wrapper_falls_back_when_dossier_build_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_fact_report,
        mock_reasoning_report,
    ):
        """Hybrid wrapper must keep deterministic output when dossier building fails."""
        monkeypatch.setenv("HFT_LLM_ENABLED", "1")
        composed = _make_composed()

        with (
            patch(_PATCH_TO_THREAD, new=AsyncMock(return_value=(mock_fact_report, mock_reasoning_report, composed))),
            patch("hft_platform.reports.pipeline.build_llm_dossier", side_effect=RuntimeError("bad dossier")),
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient") as MockClient,
            patch(_PATCH_COMPOSER) as mock_composer,
        ):
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is composed
        assert result.dossier is None
        assert result.decision is None
        assert result.llm_error == "bad dossier"
        MockReasoner.assert_not_called()
        MockClient.assert_not_called()
        mock_composer.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_wrapper_falls_back_when_hybrid_compose_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_fact_report,
        mock_reasoning_report,
    ):
        """Hybrid wrapper must keep deterministic output when LLM re-compose fails."""
        monkeypatch.setenv("HFT_LLM_ENABLED", "1")
        composed = _make_composed()
        fake_dossier = MagicMock()
        fake_llm_report = MagicMock()

        mock_composer_inst = MagicMock()

        def compose(fr, rr, llm_decision=None):
            assert fr is mock_fact_report
            assert rr is mock_reasoning_report
            assert llm_decision is fake_llm_report
            raise RuntimeError("compose fail")

        mock_composer_inst.compose.side_effect = compose

        with (
            patch(_PATCH_TO_THREAD, new=AsyncMock(return_value=(mock_fact_report, mock_reasoning_report, composed))),
            patch(_PATCH_COMPOSER, return_value=mock_composer_inst),
            patch("hft_platform.reports.pipeline.build_llm_dossier", return_value=fake_dossier),
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient"),
        ):
            MockReasoner.return_value.generate = AsyncMock(return_value=fake_llm_report)
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is composed
        assert result.dossier is None
        assert result.decision is None
        assert result.llm_error == "compose fail"
        mock_composer_inst.compose.assert_called_once_with(
            mock_fact_report,
            mock_reasoning_report,
            llm_decision=fake_llm_report,
        )

    @pytest.mark.asyncio
    async def test_async_wrapper_skips_llm_cleanly_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_fact_report,
        mock_reasoning_report,
    ):
        """Hybrid wrapper returns deterministic output without LLM work when disabled."""
        monkeypatch.setenv("HFT_LLM_ENABLED", "0")
        composed = _make_composed()

        with (
            patch(_PATCH_TO_THREAD, new=AsyncMock(return_value=(mock_fact_report, mock_reasoning_report, composed))),
            patch("hft_platform.reports.pipeline.build_llm_dossier") as mock_dossier,
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient") as MockClient,
        ):
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is composed
        assert result.dossier is None
        assert result.decision is None
        assert result.llm_error is None
        mock_dossier.assert_not_called()
        MockReasoner.assert_not_called()
        MockClient.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_wrapper_returns_none_when_sync_core_finds_empty_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Hybrid wrapper exits early for empty sessions without invoking LLM."""
        monkeypatch.setenv("HFT_LLM_ENABLED", "1")

        with (
            patch(_PATCH_TO_THREAD, new=AsyncMock(return_value=(None, None, None))),
            patch("hft_platform.reports.pipeline.build_llm_dossier") as mock_dossier,
            patch("hft_platform.reports.pipeline.LLMReportReasoner") as MockReasoner,
            patch("hft_platform.reports.pipeline.OpenRouterClient") as MockClient,
        ):
            from hft_platform.reports.pipeline import build_hybrid_report_async

            result = await build_hybrid_report_async("day", "2026-03-28", "TXFD6")

        assert result.composed is None
        assert result.dossier is None
        assert result.decision is None
        assert result.llm_error is None
        mock_dossier.assert_not_called()
        MockReasoner.assert_not_called()
        MockClient.assert_not_called()
