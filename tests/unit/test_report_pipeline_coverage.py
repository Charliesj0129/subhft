"""Coverage tests for reports/pipeline.py — uncovered run_pipeline, CLI, hybrid paths."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from hft_platform.reports.pipeline import (
    HybridReportResult,
    resolve_trading_date,
)

TZ = ZoneInfo("Asia/Taipei")


# ---------------------------------------------------------------------------
# resolve_trading_date — naive datetime path (line 77)
# ---------------------------------------------------------------------------


class TestResolveTradingDateNaiveDatetime:
    def test_naive_datetime_treated_as_taipei(self):
        """Naive datetime should be treated as Asia/Taipei."""
        naive = datetime(2026, 4, 15, 10, 0, 0)  # No tzinfo
        result = resolve_trading_date("day", now=naive)
        assert result == "2026-04-15"

    def test_naive_datetime_night_before_15(self):
        """Naive datetime before 15:00 in night session -> yesterday."""
        naive = datetime(2026, 4, 15, 3, 0, 0)  # No tzinfo, 03:00
        result = resolve_trading_date("night", now=naive)
        assert result == "2026-04-14"

    def test_naive_datetime_night_after_15(self):
        """Naive datetime after 15:00 in night session -> today."""
        naive = datetime(2026, 4, 15, 16, 0, 0)  # No tzinfo, 16:00
        result = resolve_trading_date("night", now=naive)
        assert result == "2026-04-15"


# ---------------------------------------------------------------------------
# HybridReportResult — basic construction
# ---------------------------------------------------------------------------


class TestHybridReportResult:
    def test_construct_with_all_none(self):
        result = HybridReportResult(composed=None, dossier=None, decision=None, llm_error=None)
        assert result.composed is None
        assert result.llm_error is None

    def test_construct_with_error(self):
        result = HybridReportResult(composed=None, dossier=None, decision=None, llm_error="connection failed")
        assert result.llm_error == "connection failed"


# ---------------------------------------------------------------------------
# build_hybrid_report_async — llm disabled / empty session paths
# ---------------------------------------------------------------------------


class TestBuildHybridReportAsync:
    @pytest.mark.asyncio
    async def test_empty_session_returns_none_composed(self):
        """When no tick data, returns all-None result."""
        from hft_platform.reports.pipeline import build_hybrid_report_async

        with patch(
            "hft_platform.reports.pipeline._build_report_components",
            return_value=(None, None, None),
        ):
            result = await build_hybrid_report_async("day", "2026-04-15")
        assert result.composed is None
        assert result.dossier is None
        assert result.llm_error is None

    @pytest.mark.asyncio
    async def test_llm_disabled_returns_composed_only(self, monkeypatch):
        """When HFT_LLM_ENABLED != 1, skip LLM and return composed."""
        from hft_platform.reports.pipeline import build_hybrid_report_async

        monkeypatch.delenv("HFT_LLM_ENABLED", raising=False)

        mock_composed = MagicMock()
        mock_fact = MagicMock()
        mock_reasoning = MagicMock()

        with patch(
            "hft_platform.reports.pipeline._build_report_components",
            return_value=(mock_fact, mock_reasoning, mock_composed),
        ):
            result = await build_hybrid_report_async("day", "2026-04-15")
        assert result.composed is mock_composed
        assert result.dossier is None
        assert result.llm_error is None

    @pytest.mark.asyncio
    async def test_llm_enabled_but_fails_returns_fallback(self, monkeypatch):
        """When LLM call fails, return original composed with error message."""
        from hft_platform.reports.pipeline import build_hybrid_report_async

        monkeypatch.setenv("HFT_LLM_ENABLED", "1")
        monkeypatch.setenv("HFT_LLM_MODEL", "test-model")

        mock_composed = MagicMock()
        mock_fact = MagicMock()
        mock_reasoning = MagicMock()

        with (
            patch(
                "hft_platform.reports.pipeline._build_report_components",
                return_value=(mock_fact, mock_reasoning, mock_composed),
            ),
            patch(
                "hft_platform.reports.pipeline.build_llm_dossier",
                side_effect=RuntimeError("LLM down"),
            ),
        ):
            result = await build_hybrid_report_async("day", "2026-04-15")
        assert result.composed is mock_composed
        assert result.dossier is None
        assert "LLM down" in result.llm_error

    @pytest.mark.asyncio
    async def test_llm_enabled_and_succeeds(self, monkeypatch):
        """When LLM succeeds, return hybrid composed report."""
        from hft_platform.reports.pipeline import build_hybrid_report_async

        monkeypatch.setenv("HFT_LLM_ENABLED", "1")
        monkeypatch.setenv("HFT_LLM_MODEL", "test-model")

        mock_composed = MagicMock()
        mock_hybrid_composed = MagicMock()
        mock_fact = MagicMock()
        mock_reasoning = MagicMock()
        mock_dossier = MagicMock()
        mock_decision = MagicMock()

        mock_reasoner = AsyncMock()
        mock_reasoner.generate.return_value = mock_decision

        with (
            patch(
                "hft_platform.reports.pipeline._build_report_components",
                return_value=(mock_fact, mock_reasoning, mock_composed),
            ),
            patch(
                "hft_platform.reports.pipeline.build_llm_dossier",
                return_value=mock_dossier,
            ),
            patch(
                "hft_platform.reports.pipeline.OpenRouterClient",
            ),
            patch(
                "hft_platform.reports.pipeline.LLMReportReasoner",
                return_value=mock_reasoner,
            ),
            patch(
                "hft_platform.reports.pipeline._compose_report",
                return_value=mock_hybrid_composed,
            ),
        ):
            result = await build_hybrid_report_async("day", "2026-04-15")
        assert result.composed is mock_hybrid_composed
        assert result.dossier is mock_dossier
        assert result.decision is mock_decision
        assert result.llm_error is None


# ---------------------------------------------------------------------------
# run_pipeline — dry_run and debug paths (lines 224-259)
# ---------------------------------------------------------------------------


class TestRunPipeline:
    @pytest.mark.asyncio
    async def test_run_pipeline_empty_session(self):
        """run_pipeline returns early when build_report returns None."""
        from hft_platform.reports.pipeline import run_pipeline

        with patch("hft_platform.reports.pipeline.build_report", return_value=None):
            await run_pipeline("day", "2026-04-15")

    @pytest.mark.asyncio
    async def test_run_pipeline_dry_run(self):
        """run_pipeline with dry_run skips dispatch."""
        from hft_platform.reports.pipeline import run_pipeline

        mock_composed = MagicMock()
        mock_composed.messages = []

        with patch("hft_platform.reports.pipeline.build_report", return_value=mock_composed):
            await run_pipeline("day", "2026-04-15", dry_run=True)

    @pytest.mark.asyncio
    async def test_run_pipeline_debug_mode(self):
        """run_pipeline with debug logs parts but does not dispatch."""
        from hft_platform.reports.pipeline import run_pipeline

        mock_text_part = MagicMock()
        mock_text_part.kind = "text"
        mock_text_part.min_tier = 1
        mock_text_part.content = "test content"

        mock_image_part = MagicMock()
        mock_image_part.kind = "image"
        mock_image_part.min_tier = 2
        mock_image_part.image = b"fake_image_data"
        mock_image_part.caption = "chart"

        mock_composed = MagicMock()
        mock_composed.messages = [mock_text_part, mock_image_part]

        with patch("hft_platform.reports.pipeline.build_report", return_value=mock_composed):
            await run_pipeline("day", "2026-04-15", dry_run=True, debug=True)

    @pytest.mark.asyncio
    async def test_run_pipeline_dispatch(self):
        """run_pipeline without dry_run dispatches via distributor."""
        from hft_platform.reports.pipeline import run_pipeline

        mock_composed = MagicMock()
        mock_composed.messages = []

        mock_sender = AsyncMock()
        mock_sender.close = AsyncMock()
        mock_distributor = AsyncMock()
        mock_distributor.send = AsyncMock()

        mock_dist_mod = MagicMock()
        mock_dist_mod.Distributor = MagicMock(return_value=mock_distributor)
        mock_dist_mod.ReportSender = MagicMock(return_value=mock_sender)
        mock_dist_mod.load_channels = MagicMock(return_value=[])

        with (
            patch("hft_platform.reports.pipeline.build_report", return_value=mock_composed),
            patch.dict("sys.modules", {"hft_platform.reports.distributor": mock_dist_mod}),
        ):
            await run_pipeline("day", "2026-04-15", dry_run=False)


# ---------------------------------------------------------------------------
# main — CLI entry point (lines 270-309)
# ---------------------------------------------------------------------------


class TestMainCLI:
    def test_main_disabled_exits_zero(self, monkeypatch):
        """Without HFT_REPORT_ENABLED, main exits early."""
        from hft_platform.reports.pipeline import main

        monkeypatch.delenv("HFT_REPORT_ENABLED", raising=False)
        monkeypatch.setattr("sys.argv", ["hft-reports", "--session", "day"])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    def test_main_dry_run_bypasses_enabled_check(self, monkeypatch):
        """--dry-run allows running even without HFT_REPORT_ENABLED."""
        from hft_platform.reports.pipeline import main

        monkeypatch.delenv("HFT_REPORT_ENABLED", raising=False)
        monkeypatch.setattr("sys.argv", ["hft-reports", "--session", "day", "--dry-run"])

        with patch("hft_platform.reports.pipeline.run_pipeline", new_callable=AsyncMock) as mock_run:
            with patch("hft_platform.reports.pipeline.resolve_trading_date", return_value="2026-04-15"):
                main()
        mock_run.assert_called_once()

    def test_main_debug_bypasses_enabled_check(self, monkeypatch):
        """--debug allows running even without HFT_REPORT_ENABLED."""
        from hft_platform.reports.pipeline import main

        monkeypatch.delenv("HFT_REPORT_ENABLED", raising=False)
        monkeypatch.setattr("sys.argv", ["hft-reports", "--session", "day", "--debug"])

        with patch("hft_platform.reports.pipeline.run_pipeline", new_callable=AsyncMock) as mock_run:
            with patch("hft_platform.reports.pipeline.resolve_trading_date", return_value="2026-04-15"):
                main()
        mock_run.assert_called_once()

    def test_main_with_explicit_date(self, monkeypatch):
        """--date override is passed through."""
        from hft_platform.reports.pipeline import main

        monkeypatch.setenv("HFT_REPORT_ENABLED", "1")
        monkeypatch.setattr("sys.argv", ["hft-reports", "--session", "night", "--date", "2026-04-10"])

        with patch("hft_platform.reports.pipeline.run_pipeline", new_callable=AsyncMock) as mock_run:
            main()
        mock_run.assert_called_once_with("night", "2026-04-10", dry_run=False, debug=False)

    def test_main_auto_resolves_date(self, monkeypatch):
        """Without --date, date is auto-resolved."""
        from hft_platform.reports.pipeline import main

        monkeypatch.setenv("HFT_REPORT_ENABLED", "1")
        monkeypatch.setattr("sys.argv", ["hft-reports", "--session", "day"])

        with (
            patch("hft_platform.reports.pipeline.resolve_trading_date", return_value="2026-04-15") as mock_resolve,
            patch("hft_platform.reports.pipeline.run_pipeline", new_callable=AsyncMock) as mock_run,
        ):
            main()
        mock_resolve.assert_called_once_with("day")
        mock_run.assert_called_once_with("day", "2026-04-15", dry_run=False, debug=False)
