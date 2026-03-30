"""Integration test: full three-layer pipeline with fixture data."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.reports.composer import ReportComposer
from hft_platform.reports.facts import extract_all
from hft_platform.reports.models import (
    Bar5m,
    ComposedReport,
    DaySnapshot,
    FlowBar,
    LargeTrade,
    SessionData,
)
from hft_platform.reports.pipeline import run_pipeline
from hft_platform.reports.reasoner import reason_all


def _build_fixture_session() -> SessionData:
    """Realistic fixture with enough data to exercise all extractors."""
    time_slots = [
        (h, m)
        for h in range(9, 14)
        for m in range(0, 60, 5)
    ]
    bars = [
        Bar5m(
            f"2026-03-27 {h:02d}:{m:02d}:00",
            204000000 + i * 10000,
            204200000 + i * 10000,
            203800000 + i * 10000,
            204100000 + i * 10000,
            100 + i * 5,
            50,
        )
        for i, (h, m) in enumerate(time_slots[:20])
    ]
    flow = [
        FlowBar(
            f"2026-03-27 {h:02d}:{m:02d}:00",
            50,
            100 + i * 3,
            50 + (i % 5) * 3,
            50 + max(0, 10 - (i % 5) * 2),
            0,
            (50 + (i % 5) * 3) / max(1, 50 + max(0, 10 - (i % 5) * 2)),
            (i % 5) * 3 - max(0, 10 - (i % 5) * 2),
        )
        for i, (h, m) in enumerate(time_slots[:20])
    ]
    trades = [
        LargeTrade("2026-03-27 09:15:00", 204500000, 50, "buy"),
        LargeTrade("2026-03-27 09:20:00", 204550000, 40, "sell"),
        LargeTrade("2026-03-27 10:30:00", 205000000, 80, "sell"),
        LargeTrade("2026-03-27 12:45:00", 204200000, 60, "sell"),
    ]
    return SessionData(
        session="day",
        symbol="TXFD6",
        date="2026-03-27",
        open=204000000,
        high=206500000,
        low=203500000,
        close=204200000,
        volume=15000,
        tick_count=60000,
        bars_5m=bars,
        flow_5m=flow,
        large_trades=trades,
        spread_dist={2: 5000, 3: 3000, 4: 1000},
        depth_imbalance=[],
    )


class TestFullPipeline:
    def test_produces_composed_report(self):
        sd = _build_fixture_session()
        prev_days = [
            DaySnapshot(
                "2026-03-26", "day",
                205000000, 207000000, 204000000, 206000000,
                18000, 1.15, 800,
            ),
        ]

        fr = extract_all(sd, prev_days=prev_days)
        assert len(fr.segments) >= 2
        assert fr.flow.session_ud > 0
        assert fr.volatility.atr_5m > 0

        rr = reason_all(fr)
        assert rr.bias.bias in ("bullish", "bearish", "neutral")
        assert len(rr.levels) >= 1
        assert len(rr.narrative.storyline) >= 1

        cr = ReportComposer().compose(fr, rr)
        assert isinstance(cr, ComposedReport)
        assert len(cr.messages) >= 7

        free_msgs = [m for m in cr.messages if m.min_tier == "free"]
        paid_msgs = [m for m in cr.messages if m.min_tier == "paid"]
        assert len(free_msgs) >= 2
        assert len(paid_msgs) >= 5

        for msg in cr.messages:
            if msg.kind == "text":
                assert len(msg.content) <= 4096, f"Too long: {len(msg.content)}"

    def test_with_empty_prev_days(self):
        sd = _build_fixture_session()
        fr = extract_all(sd, prev_days=[])
        rr = reason_all(fr)
        cr = ReportComposer().compose(fr, rr)
        assert isinstance(cr, ComposedReport)
        assert len(cr.messages) >= 7


class TestPipelineEntryPoint:
    @pytest.mark.asyncio
    async def test_empty_session_returns_early(self):
        empty_sd = SessionData(
            session="day", symbol="TXFD6", date="2026-03-27",
            open=0, high=0, low=0, close=0,
            volume=0, tick_count=0,
            bars_5m=[], flow_5m=[], large_trades=[],
            spread_dist={}, depth_imbalance=[],
        )
        mock_collector = MagicMock()
        mock_collector.collect = MagicMock(return_value=empty_sd)

        with patch(
            "hft_platform.reports.collector.DataCollector",
            return_value=mock_collector,
        ):
            result = await run_pipeline("day", "2026-03-27", dry_run=True)
            # Should return early without calling extract_all
            assert result is None
