"""Tests for detail panel rendering (Phase 3)."""

from __future__ import annotations

import pytest

pytest.importorskip("rich")

from hft_platform.monitor._detail_panel import build_detail_panel
from hft_platform.monitor._types import AlphaState, MonitorConfig, SymbolState, WatchlistSymbol


def _config() -> MonitorConfig:
    ws = WatchlistSymbol(
        code="2330", name="台積電", product_type="stock", alpha_ids=("alpha_1", "alpha_2")
    )
    return MonitorConfig(symbols=(ws,))


def _ss() -> SymbolState:
    ss = SymbolState(
        symbol=WatchlistSymbol(
            code="2330",
            name="台積電",
            product_type="stock",
            alpha_ids=("alpha_1", "alpha_2"),
        ),
        tick_count=200,
        composite=-2.5,
        last_price=650.0,
        spread_bps=26.0,
        bid_qty=120.0,
        ask_qty=85.0,
        ofi_l1_cum=342.0,
        last_update_ns=1_000_000_000,
    )
    a1 = AlphaState(alpha_id="alpha_1", signal=-0.31, z_score=2.1)
    a2 = AlphaState(alpha_id="alpha_2", signal=-0.15, z_score=1.4)
    ss.alpha_states = {"alpha_1": a1, "alpha_2": a2}
    return ss


def test_detail_panel_none_shows_placeholder() -> None:
    panel = build_detail_panel(None, _config(), {})
    assert "No symbol selected" in panel.renderable.plain


def test_detail_panel_shows_symbol_and_composite() -> None:
    ss = _ss()
    panel = build_detail_panel(ss, _config(), {"alpha_1": 0.5})
    text = panel.renderable.plain
    assert "ALP" in text  # truncated alpha_1 -> ALP
    assert "Composite" in text


def test_detail_panel_shows_why_line() -> None:
    ss = _ss()
    panel = build_detail_panel(ss, _config(), {})
    text = panel.renderable.plain
    assert "WHY" in text
    assert "bearish" in text


def test_detail_panel_handles_disabled_alpha() -> None:
    ss = _ss()
    ss.alpha_states["alpha_1"].disabled = True
    panel = build_detail_panel(ss, _config(), {})
    text = panel.renderable.plain
    assert "OFF" in text
