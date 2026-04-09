"""Test flow heatmap generation."""

from __future__ import annotations

from hft_platform.reports.heatmap import generate_heatmap
from hft_platform.reports.models import Bar5m, FlowBar, LargeTrade, SessionData


def _make_session_data() -> SessionData:
    bars = [
        Bar5m(
            ts=f"2026-03-27 09:{i * 5:02d}:00",
            open=205000000,
            high=205200000,
            low=204800000,
            close=205100000,
            volume=100 + i * 10,
            ticks=50,
        )
        for i in range(10)
    ]
    flow = [
        FlowBar(
            ts=f"2026-03-27 09:{i * 5:02d}:00",
            ticks=50,
            total_vol=100 + i * 10,
            uptick_vol=50 + i * 5,
            downtick_vol=50 + i * 5 - (i * 2),
            flat_vol=i * 2,
            ud_ratio=1.0 + (i - 5) * 0.1,
            net_flow=(i - 5) * 10,
        )
        for i in range(10)
    ]
    trades = [
        LargeTrade(ts="2026-03-27 09:15:00", price=205000000, volume=50, direction="buy"),
        LargeTrade(ts="2026-03-27 09:30:00", price=204800000, volume=40, direction="sell"),
    ]
    return SessionData(
        session="day",
        symbol="TXFD6",
        date="2026-03-27",
        open=205000000,
        high=205200000,
        low=204800000,
        close=205100000,
        volume=1500,
        tick_count=5000,
        bars_5m=bars,
        flow_5m=flow,
        large_trades=trades,
        spread_dist={},
        depth_imbalance=[],
    )


def test_generate_heatmap_returns_png_bytes():
    sd = _make_session_data()
    result = generate_heatmap(sd)
    assert isinstance(result, bytes)
    assert result[:4] == b"\x89PNG"
    assert len(result) > 1000


def test_generate_heatmap_empty_data():
    sd = SessionData(
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
    result = generate_heatmap(sd)
    assert result is None
