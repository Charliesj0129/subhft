"""Integration test for the full report pipeline."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.reports.models import (
    Bar5m,
    DepthBar,
    FlowBar,
    LargeTrade,
    SessionData,
)
from hft_platform.reports.pipeline import run_pipeline


def _mock_session_data() -> SessionData:
    bars = [
        Bar5m(
            ts=f"2026-03-27 {15 + i // 12}:{(i % 12) * 5:02d}:00",
            open=330000000 - i * 100000,
            high=330100000 - i * 100000,
            low=329800000 - i * 100000,
            close=329900000 - i * 100000,
            volume=500,
            ticks=300,
        )
        for i in range(24)
    ]
    flow = [
        FlowBar(
            ts=b.ts,
            ticks=300,
            total_vol=500,
            uptick_vol=200,
            downtick_vol=250,
            flat_vol=50,
            ud_ratio=0.8,
            net_flow=-50,
        )
        for b in bars
    ]
    trades = [
        LargeTrade(
            ts="2026-03-27 21:58:00",
            price=324000000,
            volume=28,
            direction="unknown",
        ),
        LargeTrade(
            ts="2026-03-27 23:31:00",
            price=327500000,
            volume=32,
            direction="unknown",
        ),
    ]
    return SessionData(
        session="night",
        symbol="TXFD6",
        date="2026-03-27",
        open=330490000,
        high=330490000,
        low=323750000,
        close=324380000,
        volume=58107,
        tick_count=38153,
        bars_5m=bars,
        flow_5m=flow,
        large_trades=trades,
        spread_dist={3: 147202, 4: 81011},
        depth_imbalance=[
            DepthBar(hour=15, avg_bid_vol=3.0, avg_ask_vol=2.8, bid_ratio=0.517)
        ],
    )


class TestPipelineIntegration:
    @pytest.mark.asyncio
    async def test_dry_run_produces_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_collector = MagicMock()
        mock_collector.collect = MagicMock(return_value=_mock_session_data())

        with patch(
            "hft_platform.reports.collector.DataCollector",
            return_value=mock_collector,
        ):
            await run_pipeline("night", "2026-03-27", dry_run=True, debug=True)

        captured = capsys.readouterr()
        assert "台指期" in captured.out
        assert "知情流" in captured.out
        assert "投資有風險" in captured.out

    @pytest.mark.asyncio
    async def test_dry_run_does_not_send(self) -> None:
        mock_collector = MagicMock()
        mock_collector.collect = MagicMock(return_value=_mock_session_data())

        with patch(
            "hft_platform.reports.collector.DataCollector",
            return_value=mock_collector,
        ), patch("hft_platform.reports.distributor.load_channels") as mock_load:
            await run_pipeline("night", "2026-03-27", dry_run=True)
            mock_load.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_session_returns_early(self) -> None:
        empty_sd = SessionData(
            session="day",
            symbol="TXFD6",
            date="2026-03-27",
            open=0,
            high=0,
            low=0,
            close=0,
            volume=0,
            tick_count=0,
            bars_5m=[],
            flow_5m=[],
            large_trades=[],
            spread_dist={},
            depth_imbalance=[],
        )
        mock_collector = MagicMock()
        mock_collector.collect = MagicMock(return_value=empty_sd)

        with patch(
            "hft_platform.reports.collector.DataCollector",
            return_value=mock_collector,
        ), patch("hft_platform.reports.signals.SignalEngine") as mock_engine:
            await run_pipeline("day", "2026-03-27", dry_run=True)
            mock_engine.assert_not_called()
